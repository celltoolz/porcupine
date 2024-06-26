"""Run commands within the Porcupine window."""
from __future__ import annotations

import locale
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter
from functools import partial
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

import psutil

from porcupine import (
    get_tab_manager,
    get_vertical_panedwindow,
    images,
    menubar,
    settings,
    textutils,
    utils,
)
from porcupine.plugins.run import common
from porcupine.settings import global_settings
from porcupine.textutils import add_change_blocker, track_changes
from porcupine.utils import copy_type

log = logging.getLogger(__name__)


filename_regex_parts = [
    # c compiler output, also many other tools
    # TODO: support spaces in file names?
    # playground.c:4:9: warning: ...
    r"([^\n\s:()]+):([0-9]+)",
    # python error
    r'File "([^\n"]+)", line ([0-9]+)',
    # valgrind, SDL_assert() etc
    # blah blah: some_function (filename.c:123)
    r"\(([^\n():]+):([0-9]+)\)",
]
filename_regex = "(?i)" + "|".join(r"(?:" + part + r")" for part in filename_regex_parts)


def open_file_with_line_number(path: Path, lineno: int) -> None:
    tab = get_tab_manager().open_file(path)
    if tab is not None:
        tab.textwidget.mark_set("insert", f"{lineno}.0")
        tab.textwidget.see("insert")
        tab.textwidget.tag_remove("sel", "1.0", "end")
        tab.textwidget.tag_add("sel", "insert", "insert lineend")


MAX_SCROLLBACK = 5000
OUTPUT_TAGS = {"info": "Token.Keyword", "output": "Token.Text", "error": "Token.Name.Exception"}


class Executor:
    def __init__(self, cwd: Path, textwidget: tkinter.Text, link_manager: textutils.LinkManager):
        self.cwd = cwd
        self._textwidget = textwidget
        self._link_manager = link_manager

        self._shell_process: subprocess.Popen[bytes] | None = None
        self._input_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)
        self._timeout_id: str | None = None
        self.started = False
        self.paused = False
        self._thread: threading.Thread | None = None

    def run(self, command: str) -> None:
        env = common.prepare_env()
        env["PYTHONUNBUFFERED"] = "1"  # same as passing -u option to python (#802)

        # Update needed to get width and height, but causes key bindings to execute
        self._textwidget.update()
        width, height = textutils.textwidget_size(self._textwidget)

        font = tkinter.font.Font(name="TkFixedFont", exists=True)
        env["COLUMNS"] = str(width // font.measure("a"))
        env["LINES"] = str(height // font.metrics("linespace"))

        self._thread = threading.Thread(
            target=self._thread_target, args=[command, env], daemon=True
        )
        self._thread.start()
        self._poll_queue_and_put_to_textwidget()
        self.started = True

    @property
    def running(self) -> bool:
        return self._shell_process is not None and self._shell_process.poll() is None

    def _thread_target(self, command: str, env: dict[str, str]) -> None:
        self._queue.put(("info", command + "\n"))

        try:
            self._shell_process = subprocess.Popen(
                command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=True,
                env=env,
                **utils.subprocess_kwargs,
            )
        except OSError as e:
            self._queue.put(("error", f"{type(e).__name__}: {e}\n"))
            log.debug("here's full traceback", exc_info=True)
            return

        assert self._shell_process.stdout is not None
        while True:
            bytez = self._shell_process.stdout.read1()  # type: ignore
            if not bytez:
                break
            text = bytez.decode(locale.getpreferredencoding(), errors="replace")
            self._queue.put(("output", utils.tkinter_safe_string(text).replace("\r\n", "\n")))

        status = self._shell_process.wait()
        if status == 0:
            self._queue.put(("info", "The process completed successfully."))
        else:
            self._queue.put(("error", f"The process failed with status {status}."))
        self._queue.put(("end", ""))

    def _handle_queued_item(self, message_type: str, text: str) -> None:
        if message_type == "end":
            assert not text
            get_tab_manager().event_generate("<<FileSystemChanged>>")
        else:
            scrolled_to_end = self._textwidget.yview()[1] == 1.0

            self._textwidget.insert("end", text, [OUTPUT_TAGS[message_type], "uneditable"])
            # Add links to full lines
            linked_line_count = text.count("\n")
            self._link_manager.add_links(
                start=f"end - 1 char linestart - {linked_line_count} lines",
                end="end - 1 char linestart",
            )
            if scrolled_to_end:
                self._textwidget.yview_moveto(1)

        self._textwidget.delete("1.0", f"end - {MAX_SCROLLBACK} lines")

    def _poll_queue_and_put_to_textwidget(self) -> None:
        # too many iterations here freezes the GUI when an infinite loop with print is running
        for iteration in range(10):
            try:
                message_type, text = self._queue.get(block=False)
            except queue.Empty:
                break
            self._handle_queued_item(message_type, text)

        self._timeout_id = self._textwidget.after(50, self._poll_queue_and_put_to_textwidget)

    def send_signal(self, signal: signal.Signals) -> None:
        if self._shell_process is None:
            return

        try:
            process = psutil.Process(self._shell_process.pid)
            process.send_signal(signal)
            for child in process.children():
                try:
                    child.send_signal(signal)
                except psutil.NoSuchProcess:
                    pass

        except (psutil.NoSuchProcess, ProcessLookupError):
            pass

    def stop(self, *, quitting: bool = False) -> None:
        if self._timeout_id is not None:
            self._textwidget.after_cancel(self._timeout_id)
            self._timeout_id = None

        if self._shell_process is None:
            return

        try:
            # On non-windows we can stop the shell so that it can't spawn more children
            # On windows there is a race condition
            if sys.platform != "win32":
                self._shell_process.send_signal(signal.SIGSTOP)

            # If we kill the shell, its child processes will keep running.
            # But they will reparent to pid 1 so can no longer list them
            children = psutil.Process(self._shell_process.pid).children()
            if self._shell_process.poll() is None:
                # shell still alive, the pid wasn't reused
                self._shell_process.kill()
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        # Child already dead, but we need to kill other children
                        pass

        # shell can die at any time
        # non-psutil errors happen in langserver plugin, not sure if needed here
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass

        else:
            if not quitting:
                assert self._thread is not None
                assert self._thread.is_alive()

                if sys.platform == "win32":
                    kill_status = 1
                else:
                    kill_status = -signal.SIGKILL

                # Consume queue until the thread stops
                while True:
                    message_type, text = self._queue.get(block=True)
                    # For killing messages, a separate "Killed." will be added below
                    if (message_type, text) != (
                        "error",
                        f"The process failed with status {kill_status}.",
                    ):
                        self._handle_queued_item(message_type, text)
                    if message_type == "end":
                        break
                self._thread.join()

                self._handle_queued_item("error", "Killed.")

        if not quitting:
            get_tab_manager().event_generate("<<FileSystemChanged>>")

    def write_to_stdin(self, data: bytes) -> None:
        if self._shell_process is not None:
            assert self._shell_process.stdin is not None
            self._shell_process.stdin.write(data)
            self._shell_process.stdin.flush()  # TODO: may block, maybe that isn't an issue in practice?

    def close_stdin(self) -> None:
        if self._shell_process is not None:
            assert self._shell_process.stdin is not None
            self._shell_process.stdin.close()


# A python-edit-tracking text widget lets you know whether the text is currently
# being edited through invoking a method in Python, such as .insert(). This lets
# you distinguish between default key bindings and whatever your code does.
class PyEditTrackingText(tkinter.Text):
    @copy_type(tkinter.Text.__init__)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.in_a_python_method = False

    @copy_type(tkinter.Text.insert)
    def insert(self, *args: Any, **kwargs: Any) -> Any:
        self.in_a_python_method = True
        try:
            return super().insert(*args, **kwargs)
        finally:
            self.in_a_python_method = False

    @copy_type(tkinter.Text.delete)
    def delete(self, *args: Any, **kwargs: Any) -> Any:
        self.in_a_python_method = True
        try:
            return super().delete(*args, **kwargs)
        finally:
            self.in_a_python_method = False

    @copy_type(tkinter.Text.replace)
    def replace(self, *args: Any, **kwargs: Any) -> Any:
        self.in_a_python_method = True
        try:
            return super().replace(*args, **kwargs)
        finally:
            self.in_a_python_method = False


class NoTerminalRunner:
    def __init__(self, master: tkinter.Misc) -> None:
        self.textwidget = PyEditTrackingText(
            master,
            name="run_output",  # TODO: rename
            font="TkFixedFont",
            blockcursor=True,
            insertunfocussed="hollow",
            wrap="char",
        )

        textutils.use_pygments_tags(self.textwidget, option_name="run_output_pygments_style")

        def on_style_changed(junk: object = None) -> None:
            self.textwidget.config(
                foreground=(
                    self.textwidget.tag_cget("Token.Literal.String", "foreground")
                    or self.textwidget["fg"]
                )
            )

        self.textwidget.bind(
            "<<GlobalSettingChanged:run_output_pygments_style>>", on_style_changed, add=True
        )
        on_style_changed()

        self.textwidget.bind("<Destroy>", partial(self.stop_executor, quitting=True), add=True)
        self.textwidget.bind("<Control-D>", self._handle_end_of_input, add=True)
        self.textwidget.bind("<Control-d>", self._handle_end_of_input, add=True)
        self.textwidget.bind("<Return>", self.handle_enter_press, add=True)
        self.textwidget.bind("<BackSpace>", self._handle_backspace, add=True)

        track_changes(self.textwidget)
        add_change_blocker(self.textwidget, self._editing_should_be_blocked)

        self._link_manager = textutils.LinkManager(
            self.textwidget, filename_regex, self._get_link_opener
        )
        self.textwidget.tag_config("link", underline=True)
        self.executor: Executor | None = None

        button_frame = tkinter.Frame(self.textwidget)
        button_frame.place(relx=1, rely=0, anchor="ne")
        settings.use_pygments_fg_and_bg(
            button_frame,
            (lambda fg, bg: button_frame.config(bg=bg)),
            option_name="run_output_pygments_style",
        )

        self.pause_button = ttk.Label(button_frame, image=images.get("pause"), cursor="hand2")
        if sys.platform != "win32":
            self.pause_button.pack(side="left", padx=1)
        utils.set_tooltip(self.pause_button, "Pause execution")
        self.stop_button = ttk.Label(button_frame, image=images.get("stop"), cursor="hand2")
        self.stop_button.pack(side="left", padx=1)
        utils.set_tooltip(self.stop_button, "Kill Process")
        self.hide_button = ttk.Label(button_frame, image=images.get("closebutton"), cursor="hand2")
        self.hide_button.pack(side="left", padx=1)
        utils.set_tooltip(self.hide_button, "Hide output")

    def _editing_should_be_blocked(self) -> bool:
        return (not self.textwidget.in_a_python_method) and (
            # Block editing when nothing is running
            self.executor is None
            or not self.executor.running
            # Do not edit uneditable text
            or "uneditable" in self.textwidget.tag_names("insert")
        )

    def handle_enter_press(self, junk_event: object | None = None) -> str:
        if not self._editing_should_be_blocked():
            assert self.executor is not None  # helps mypy

            self.textwidget.mark_set("insert", "insert lineend")
            self.textwidget.insert("end - 1 char", "\n")
            self.textwidget.see("insert")

            input_line = self.textwidget.get("uneditable.last", "end - 1 char").replace(
                "\n", os.linesep
            )
            self.textwidget.tag_add("uneditable", "1.0", "end - 1 char")
            self.executor.write_to_stdin(
                input_line.encode(locale.getpreferredencoding(), errors="replace")
            )

        return "break"

    def _handle_backspace(self, event: tkinter.Event[tkinter.Text]) -> str:
        if "uneditable" not in self.textwidget.tag_names("insert - 1 char"):
            self.textwidget.delete("insert - 1 char", "insert")
        return "break"

    def _handle_end_of_input(self, event: tkinter.Event[tkinter.Text]) -> None:
        if self.executor is not None:
            self.executor.close_stdin()

    def stop_executor(self, junk_event: object = None, *, quitting: bool = False) -> None:
        if self.executor is not None:
            self.executor.stop(quitting=quitting)

    def pause_resume_executor(self, junk: object = None) -> None:
        if sys.platform != "win32" and self.executor is not None and self.executor.running:
            if self.executor.paused:
                self.executor.send_signal(signal.SIGCONT)
                self.executor.paused = False
                self.pause_button.configure(image=images.get("pause"))
                utils.set_tooltip(self.pause_button, "Pause execution")

            else:
                self.executor.send_signal(signal.SIGSTOP)
                self.executor.paused = True
                self.pause_button.configure(image=images.get("resume"))
                utils.set_tooltip(self.pause_button, "Resume execution")

    def focus(self, junk: object = None) -> None:
        is_hidden = get_vertical_panedwindow().panecget(self.textwidget, "hide")
        if is_hidden:
            get_vertical_panedwindow().paneconfigure(self.textwidget, hide=False)
        self.textwidget.focus()

    def _get_link_opener(self, match: re.Match[str]) -> Callable[[], None] | None:
        assert self.executor is not None

        filename, lineno = (value for value in match.groups() if value is not None)
        path = self.executor.cwd / filename  # doesn't use cwd if filename is absolute

        try:
            if not path.is_file():
                return None
        except OSError:
            # e.g. filename too long
            return None

        return partial(open_file_with_line_number, path, int(lineno))

    def run_command(self, cwd: Path, command: str) -> None:
        if self.executor is not None:
            # This prevents a bug where smashing F5 runs in parallel
            if not self.executor.started:
                return

            self.executor.stop()

        self.textwidget.delete("1.0", "end")
        self._link_manager.delete_all_links()  # prevent memory leak
        self.pause_button.configure(image=images.get("pause"))

        self.executor = Executor(cwd, self.textwidget, self._link_manager)
        self.executor.run(command)


runner: NoTerminalRunner | None = None


def setup() -> None:
    global_settings.add_option("run_output_pygments_style", default="inkpot")
    settings.add_pygments_style_button(
        "run_output_pygments_style", "Pygments style for output of commands:"
    )

    global runner
    assert runner is None
    runner = NoTerminalRunner(get_vertical_panedwindow())
    get_vertical_panedwindow().add(
        runner.textwidget, after=get_tab_manager(), stretch="never", hide=True
    )
    settings.remember_pane_size(
        get_vertical_panedwindow(), runner.textwidget, "run_command_output_height", 200
    )

    def toggle_visible(junk_event: object = ...) -> None:
        assert runner is not None
        is_hidden = get_vertical_panedwindow().panecget(runner.textwidget, "hide")
        get_vertical_panedwindow().paneconfigure(runner.textwidget, hide=not is_hidden)

    runner.hide_button.bind("<Button-1>", toggle_visible, add=True)
    runner.stop_button.bind("<Button-1>", runner.stop_executor, add=True)
    runner.pause_button.bind("<Button-1>", runner.pause_resume_executor, add=True)
    menubar.get_menu("Run").add_command(label="Show/hide output", command=toggle_visible)
    menubar.get_menu("View/Focus").add_command(label="Command output", command=runner.focus)
    if sys.platform != "win32":
        menubar.get_menu("Run").add_command(
            label="Pause/resume process", command=runner.pause_resume_executor
        )
    menubar.get_menu("Run").add_command(label="Kill process", command=runner.stop_executor)


# Use previous height as long as it's not less than three lines high,
# otherwise set height to three lines high.
def set_output_height() -> None:
    assert runner is not None
    current_height = textutils.textwidget_size(runner.textwidget)[1]
    padding = textutils.get_padding(runner.textwidget)[1]
    linespace = tkinter.font.Font(font="TkFixedFont").metrics("linespace")
    minimum_height = 3 * linespace + 2 * padding

    if current_height < minimum_height:
        # No idea why, but setting the height once doesn't always work on Akuli's system.
        get_vertical_panedwindow().paneconfigure(runner.textwidget, height=minimum_height)
        get_vertical_panedwindow().update()
        get_vertical_panedwindow().paneconfigure(runner.textwidget, height=minimum_height)


def run_command(command: str, cwd: Path) -> None:
    log.info(f"Running {command} in {cwd}")
    assert runner is not None
    get_vertical_panedwindow().paneconfigure(runner.textwidget, hide=False)
    runner.run_command(cwd, command)
    set_output_height()
