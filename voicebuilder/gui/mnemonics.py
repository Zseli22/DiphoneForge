"""Reliable cross-container `Alt+<key>` mnemonic dispatching for wxPython on Windows.

On wxMSW, native `Alt+<letter>` mnemonic lookup does not reliably cross `wx.Simplebook`
boundaries — so pressing `Alt+N` (`&Next >`) or `Alt+B` (`< &Back`) while focus is inside
a wizard step panel plays a Windows error ding instead of activating the button.

This module inspects ONLY the currently visible controls of a top-level window (skipping
hidden `wx.Simplebook` pages), resolves `&<key>` mnemonics consistently across nested
panels and books, and activates the matching button/checkbox/radio or focuses the target
control paired with a `wx.StaticText` label.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional, Tuple

import wx

_MNEMONIC_RE = re.compile(r"&([A-Za-z0-9])")

_FOCUSABLE_TYPES = (
    wx.TextCtrl,
    wx.Choice,
    wx.ComboBox,
    wx.SpinCtrl,
    wx.ListCtrl,
    wx.ListBox,
    wx.CheckBox,
    wx.RadioButton,
    wx.Button,
)


def extract_mnemonic(label: str) -> Optional[str]:
    """Return the uppercase mnemonic character from a wx label (ignoring `&&` escapes)."""
    if not label:
        return None
    m = _MNEMONIC_RE.search(label.replace("&&", ""))
    return m.group(1).upper() if m else None


def iter_visible_controls(root: wx.Window) -> Iterator[wx.Window]:
    """Yield all currently shown descendants of `root`, entering only the active page of books."""
    for child in root.GetChildren():
        if not child.IsShown():
            continue
        if isinstance(child, wx.BookCtrlBase):
            sel = child.GetSelection()
            if sel != wx.NOT_FOUND:
                page = child.GetPage(sel)
                if page is not None and page.IsShown():
                    yield from iter_visible_controls(page)
            continue
        yield child
        if child.GetChildren():
            yield from iter_visible_controls(child)


def _find_label_target(label_ctrl: wx.StaticText) -> Optional[wx.Window]:
    """Find the next visible, enabled focusable sibling after a `wx.StaticText` label."""
    parent = label_ctrl.GetParent()
    if parent is None:
        return None
    siblings: List[wx.Window] = list(parent.GetChildren())
    try:
        idx = siblings.index(label_ctrl)
    except ValueError:
        return None
    for sib in siblings[idx + 1 :]:
        if not sib.IsShown():
            continue
        if isinstance(sib, _FOCUSABLE_TYPES) and sib.IsEnabled():
            return sib
        if isinstance(sib, wx.Panel) and not isinstance(sib, wx.BookCtrlBase):
            for nested in iter_visible_controls(sib):
                if isinstance(nested, _FOCUSABLE_TYPES) and nested.IsEnabled():
                    return nested
    return None


def _key_char_from_event(evt: wx.KeyEvent) -> Optional[str]:
    uk = evt.GetUnicodeKey()
    if uk and uk != wx.WXK_NONE:
        ch = chr(uk).upper()
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
            return ch
    code = evt.GetKeyCode()
    if (ord("A") <= code <= ord("Z")) or (ord("a") <= code <= ord("z")) or (ord("0") <= code <= ord("9")):
        return chr(code).upper()
    return None


def dispatch_alt_mnemonic(root: wx.Window, evt: wx.KeyEvent) -> bool:
    """Handle `Alt+<alnum>` across the entire visible control tree of `root`.

    Returns `True` if a matching visible mnemonic was found and dispatched (caller should
    NOT call `evt.Skip()`, preventing Windows error dings). Returns `False` otherwise.
    """
    if not evt.AltDown() or evt.ControlDown() or evt.MetaDown():
        return False

    key = _key_char_from_event(evt)
    if key is None:
        return False

    # Collect matching visible controls; prefer enabled interactive controls over disabled ones
    matches: List[Tuple[int, wx.Window]] = []
    for ctrl in iter_visible_controls(root):
        if isinstance(ctrl, (wx.Button, wx.CheckBox, wx.RadioButton, wx.StaticText)):
            mn = extract_mnemonic(ctrl.GetLabel())
            if mn == key:
                prio = 0 if isinstance(ctrl, (wx.Button, wx.CheckBox, wx.RadioButton)) else 1
                matches.append((prio, ctrl))

    if not matches:
        return False

    matches.sort(key=lambda x: x[0])

    for _prio, ctrl in matches:
        if isinstance(ctrl, wx.Button):
            if not ctrl.IsEnabled():
                continue
            cmd = wx.CommandEvent(wx.EVT_BUTTON.typeId, ctrl.GetId())
            cmd.SetEventObject(ctrl)
            wx.PostEvent(ctrl, cmd)
            return True

        if isinstance(ctrl, wx.CheckBox):
            if not ctrl.IsEnabled():
                continue
            ctrl.SetFocus()
            new_val = not ctrl.GetValue()
            ctrl.SetValue(new_val)
            cmd = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, ctrl.GetId())
            cmd.SetEventObject(ctrl)
            cmd.SetInt(int(new_val))
            wx.PostEvent(ctrl, cmd)
            return True

        if isinstance(ctrl, wx.RadioButton):
            if not ctrl.IsEnabled():
                continue
            ctrl.SetFocus()
            ctrl.SetValue(True)
            cmd = wx.CommandEvent(wx.EVT_RADIOBUTTON.typeId, ctrl.GetId())
            cmd.SetEventObject(ctrl)
            cmd.SetInt(1)
            wx.PostEvent(ctrl, cmd)
            return True

        if isinstance(ctrl, wx.StaticText):
            target = _find_label_target(ctrl)
            if target is not None:
                target.SetFocus()
                return True

    return False


def install_mnemonic_handler(root: wx.Window) -> None:
    """Bind cross-container `Alt+<key>` mnemonic dispatch onto a top-level frame or dialog."""

    def _on_char_hook(evt: wx.KeyEvent) -> None:
        if dispatch_alt_mnemonic(root, evt):
            return
        evt.Skip()

    root.Bind(wx.EVT_CHAR_HOOK, _on_char_hook)
