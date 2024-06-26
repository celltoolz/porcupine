"""Run commands in a separate terminal or with output going into the Porcupine window.

See the "Run" menu in the menubar.
"""
from __future__ import annotations

import sys
import tkinter
from functools import partial
from tkinter import messagebox

from porcupine import get_main_window, get_tab_manager, menubar, tabs, utils
from porcupine.plugins import python_venv

from . import common, dialog, history, no_terminal, terminal

# affects order of buttons in setting dialog, want pygments buttons together
setup_before = ["filetypes"]


def run(ctx: common.Context, command: common.Command) -> None:
    history.add(ctx, command)

    venv = python_venv.get_venv(ctx.project_path)
    if venv is None:
        command_string = command.format_command()
    else:
        if sys.platform == "win32":
            activate = utils.quote(str(venv / "Scripts" / "activate"))
            # https://stackoverflow.com/a/8055390
            command_string = f"{activate} & {command.format_command()}"
        else:
            activate = utils.quote(str(venv / "bin" / "activate"))
            command_string = f". {activate}\n{command.format_command()}"

    if command.external_terminal:
        terminal.run_command(command_string, command.format_cwd())
    else:
        no_terminal.run_command(command_string, command.format_cwd())


def ask_and_run_command(initial_key_id: int, junk_event: tkinter.Event[tkinter.Misc]) -> None:
    tab = get_tab_manager().select()
    if not isinstance(tab, tabs.FileTab) or not tab.save():
        return

    ctx = common.Context(tab, initial_key_id)
    ask_result = dialog.ask_command(ctx)
    if ask_result is not None:
        command, chosen_key_id = ask_result
        ctx.key_id = chosen_key_id
        run(ctx, command)


def repeat_command(key_id: int, junk_event: tkinter.Event[tkinter.Misc]) -> None:
    tab = get_tab_manager().select()
    if not isinstance(tab, tabs.FileTab) or not tab.save():
        return

    ctx = common.Context(tab, key_id)
    command = history.get_command_to_repeat(ctx)
    if command is None:
        ask = utils.get_binding(f"<<Run:AskAndRun{key_id}>>")
        repeat = utils.get_binding(f"<<Run:Repeat{key_id}>>")
        messagebox.showerror(
            "No commands to repeat",
            f"Please press {ask} to choose a command to run. You can then repeat it with {repeat}.",
        )
    else:
        run(ctx, command)


def on_new_filetab(tab: tabs.FileTab) -> None:
    tab.settings.add_option("example_commands", [], type_=list[common.ExampleCommand])


def setup() -> None:
    get_tab_manager().add_filetab_callback(on_new_filetab)

    menubar.add_filetab_command(
        "Run/Run command",
        (lambda tab: get_main_window().event_generate("<<Run:AskAndRun0>>")),
        accelerator=utils.get_binding("<<Run:AskAndRun0>>", menu=True),
    )
    menubar.add_filetab_command(
        "Run/Repeat previous command",
        (lambda tab: get_main_window().event_generate("<<Run:Repeat0>>")),
        accelerator=utils.get_binding("<<Run:Repeat0>>", menu=True),
    )

    for key_id in range(4):
        get_main_window().bind(
            f"<<Run:AskAndRun{key_id}>>", partial(ask_and_run_command, key_id), add=True
        )
        get_main_window().bind(f"<<Run:Repeat{key_id}>>", partial(repeat_command, key_id), add=True)

    no_terminal.setup()
