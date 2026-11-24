"""Screen 2 — Dual-Mode Audio Studio (Built-in PC Mic Studio + External Recorder Import).

Built-in Mic Studio UX invariants:
  - Space toggles recording Start / Stop (when not focused inside an editable text field).
  - P plays the eSpeak pronunciation reference on demand for real words.
  - Real-words -> Nonsense-supplements transition gate:
      * Force-stops any active recording cleanly.
      * Alerts the speaker via an accessible dialog that the remaining prompts are
        nonsense sounds and they should listen carefully.
      * Immediately auto-plays the eSpeak audio reference on every nonsense supplement line!
  - Instant post-take quality check (clipping, quiet, too short, SNR) reported clearly.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import wx

from ..aligner import read_ranges_file, validate_external_ranges, write_ranges_file
from ..recorder import (
    TakeRecorder,
    calibrate_room_noise,
    default_input_device_index,
    list_input_devices,
    play_wav_file,
    stop_playback,
)
from ..reference import export_reference_wavs, play_prompt_reference

if TYPE_CHECKING:
    from .app import WizardFrame


class StudioPanel(wx.Panel):
    """Step 2: Record prompts with PC mic OR import external recorder WAVs + strict range map."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame
        self.recorder = TakeRecorder()
        self.calibrated_noise_db: Optional[float] = None
        self.devices = []
        self.lines: List[Dict[str, object]] = []
        self.vowels: List[str] = []
        self.filtered_indices: List[int] = []
        self.cursor: int = 0
        self.flagged_line_set: set[int] = set()
        self._last_prompt_was_word: Optional[bool] = None
        self._nonsense_notice_shown: bool = False
        self._ext_ranges: List[Tuple[str, int, int]] = []

        root = wx.BoxSizer(wx.VERTICAL)

        # Workflow mode selector
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_mode = wx.StaticText(self, label="Recording &Workflow:")
        self.choice_mode = wx.Choice(
            self,
            choices=[
                "Built-in PC Microphone Studio",
                "External Recorder Import (long WAVs + range map)",
            ],
        )
        self.choice_mode.SetName("Recording Workflow")
        self.choice_mode.Bind(wx.EVT_CHOICE, self._on_mode_changed)
        mode_row.Add(lbl_mode, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        mode_row.Add(self.choice_mode, 0)
        root.Add(mode_row, 0, wx.BOTTOM, 8)

        self.mode_book = wx.Simplebook(self)
        self.mic_page = self._build_mic_page(self.mode_book)
        self.ext_page = self._build_external_page(self.mode_book)
        self.mode_book.AddPage(self.mic_page, "Mic Studio")
        self.mode_book.AddPage(self.ext_page, "External Import")
        root.Add(self.mode_book, 1, wx.EXPAND)

        self.SetSizer(root)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    # ------------------------------------------------------------------ Built-in Mic Studio UI
    def _build_mic_page(self, parent: wx.Window) -> wx.Panel:
        p = wx.Panel(parent)
        s = wx.BoxSizer(wx.VERTICAL)

        # Mic device & room calibration row
        dev_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_mic = wx.StaticText(p, label="&Microphone:")
        self.choice_mic = wx.Choice(p)
        self.choice_mic.SetName("Microphone Input Device")
        self.btn_mic_check = wx.Button(p, label="Mic &Test (2.5 s room tone)")
        self.btn_mic_check.Bind(wx.EVT_BUTTON, self._on_mic_check)
        dev_row.Add(lbl_mic, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        dev_row.Add(self.choice_mic, 1, wx.RIGHT, 8)
        dev_row.Add(self.btn_mic_check, 0)
        s.Add(dev_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Filter & progress row
        flt_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_flt = wx.StaticText(p, label="Show Pr&ompts:")
        self.choice_filter = wx.Choice(p, choices=["All Prompts", "Unrecorded Only", "Flagged from Audit"])
        self.choice_filter.SetName("Prompt Filter")
        self.choice_filter.SetSelection(0)
        self.choice_filter.Bind(wx.EVT_CHOICE, lambda _e: self._rebuild_filter(reset_cursor=True))
        self.lbl_progress = wx.StaticText(p, label="Recorded: 0 / 0")
        flt_row.Add(lbl_flt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        flt_row.Add(self.choice_filter, 0, wx.RIGHT, 16)
        flt_row.Add(self.lbl_progress, 1, wx.ALIGN_CENTER_VERTICAL)
        s.Add(flt_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Current prompt display box
        prompt_box = wx.StaticBox(p, label="Current Prompt (Shortcuts: Space = Record/Stop, P = Play Ref, L = Listen, Enter = Next, R = Retake)")
        ps = wx.StaticBoxSizer(prompt_box, wx.VERTICAL)

        self.lbl_prompt_header = wx.StaticText(p, label="Line 0 / 0")
        self.lbl_prompt_header.SetFont(self.lbl_prompt_header.GetFont().Bold())
        ps.Add(self.lbl_prompt_header, 0, wx.ALL, 6)

        self.txt_prompt = wx.TextCtrl(p, style=wx.TE_READONLY | wx.TE_CENTER)
        self.txt_prompt.SetName("Current Prompt Word")
        self.txt_prompt.SetFont(self.txt_prompt.GetFont().Bold().Scaled(2.0))
        self.txt_prompt.SetMinSize((-1, 56))
        ps.Add(self.txt_prompt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.lbl_prompt_ipa = wx.StaticText(p, label="")
        ps.Add(self.lbl_prompt_ipa, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Action buttons
        act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_play_ref = wx.Button(p, label="&Play Reference (P)")
        self.btn_record = wx.Button(p, label="&Start Recording (Space)")
        self.btn_listen = wx.Button(p, label="&Listen Take (L)")
        self.btn_accept = wx.Button(p, label="&Accept && Next (Enter)")
        self.btn_prev = wx.Button(p, label="Pre&vious")
        self.btn_skip = wx.Button(p, label="S&kip")

        self.btn_play_ref.Bind(wx.EVT_BUTTON, lambda _e: self._play_current_reference())
        self.btn_record.Bind(wx.EVT_BUTTON, lambda _e: self._toggle_recording())
        self.btn_listen.Bind(wx.EVT_BUTTON, lambda _e: self._listen_current_take())
        self.btn_accept.Bind(wx.EVT_BUTTON, lambda _e: self._accept_and_advance())
        self.btn_prev.Bind(wx.EVT_BUTTON, lambda _e: self._move_cursor(-1))
        self.btn_skip.Bind(wx.EVT_BUTTON, lambda _e: self._move_cursor(+1))

        for b in (self.btn_play_ref, self.btn_record, self.btn_listen, self.btn_accept, self.btn_prev, self.btn_skip):
            act_row.Add(b, 0, wx.RIGHT, 6)
        ps.Add(act_row, 0, wx.ALL, 6)

        self.txt_verdict = wx.TextCtrl(p, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 54))
        self.txt_verdict.SetName("Take Quality Verdict")
        ps.Add(self.txt_verdict, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        s.Add(ps, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Prompt jump list
        lbl_jump = wx.StaticText(p, label="Prompt L&ist (select any line to jump):")
        self.list_studio = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_studio.SetName("Studio Prompt List")
        self.list_studio.InsertColumn(0, "Line", width=65)
        self.list_studio.InsertColumn(1, "Type", width=95)
        self.list_studio.InsertColumn(2, "Prompt", width=210)
        self.list_studio.InsertColumn(3, "Status", width=180)
        self.list_studio.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_studio_list_activate)
        s.Add(lbl_jump, 0, wx.BOTTOM, 4)
        s.Add(self.list_studio, 1, wx.EXPAND)

        p.SetSizer(s)
        return p

    # ------------------------------------------------------------------ External Recorder Import UI
    def _build_external_page(self, parent: wx.Window) -> wx.Panel:
        p = wx.Panel(parent)
        s = wx.BoxSizer(wx.VERTICAL)

        exp_box = wx.StaticBox(p, label="1. Export Script & Audio Reference for External Recording")
        es = wx.StaticBoxSizer(exp_box, wx.HORIZONTAL)
        btn_open_script = wx.Button(p, label="Open &Script Folder")
        btn_exp_supp = wx.Button(p, label="Export Nonsense Re&ference WAVs")
        btn_exp_all = wx.Button(p, label="Export A&ll Reference WAVs")
        btn_open_script.Bind(wx.EVT_BUTTON, self._on_open_plan_folder)
        btn_exp_supp.Bind(wx.EVT_BUTTON, lambda _e: self._on_export_refs(only_supplements=True))
        btn_exp_all.Bind(wx.EVT_BUTTON, lambda _e: self._on_export_refs(only_supplements=False))
        es.Add(btn_open_script, 0, wx.ALL, 6)
        es.Add(btn_exp_supp, 0, wx.ALL, 6)
        es.Add(btn_exp_all, 0, wx.ALL, 6)
        s.Add(es, 0, wx.EXPAND | wx.BOTTOM, 8)

        imp_box = wx.StaticBox(p, label="2. Import External Recording WAV(s) & Strict Line Ranges")
        ims = wx.StaticBoxSizer(imp_box, wx.VERTICAL)

        self.list_ranges = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_ranges.SetName("External Recording Range Table")
        self.list_ranges.InsertColumn(0, "WAV File (in recordings/external/)", width=300)
        self.list_ranges.InsertColumn(1, "First Line", width=100)
        self.list_ranges.InsertColumn(2, "Last Line", width=100)
        self.list_ranges.SetMinSize((-1, 140))
        self.list_ranges.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit_range)
        ims.Add(self.list_ranges, 1, wx.EXPAND | wx.ALL, 6)

        rbtns = wx.BoxSizer(wx.HORIZONTAL)
        btn_add_wav = wx.Button(p, label="&Add Recording WAV...")
        btn_edit_rng = wx.Button(p, label="&Edit Selected Range...")
        btn_del_rng = wx.Button(p, label="&Remove")
        btn_val_rng = wx.Button(p, label="&Validate && Save Ranges")
        btn_add_wav.Bind(wx.EVT_BUTTON, self._on_add_external_wav)
        btn_edit_rng.Bind(wx.EVT_BUTTON, self._on_edit_range)
        btn_del_rng.Bind(wx.EVT_BUTTON, self._on_remove_range)
        btn_val_rng.Bind(wx.EVT_BUTTON, self._on_validate_ranges)
        for b in (btn_add_wav, btn_edit_rng, btn_del_rng, btn_val_rng):
            rbtns.Add(b, 0, wx.RIGHT, 6)
        ims.Add(rbtns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.txt_range_status = wx.TextCtrl(p, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 56))
        self.txt_range_status.SetName("External Range Validation Status")
        ims.Add(self.txt_range_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        s.Add(ims, 1, wx.EXPAND)

        p.SetSizer(s)
        return p

    # ------------------------------------------------------------------ Lifecycle & helpers
    def on_enter(self, *, flagged_only: Optional[Sequence[int]] = None) -> None:
        proj = self.frame.project
        if proj is None:
            return
        self._load_plan_data()
        self._refresh_mic_devices()
        self._load_audit_flags()

        if flagged_only is not None:
            self.flagged_line_set = {int(x) for x in flagged_only}
            proj.config.recording_mode = "mic"
            proj.save()
            self.choice_mode.SetSelection(0)
            self.mode_book.SetSelection(0)
            self.choice_filter.SetSelection(2)
        else:
            mode_idx = 0 if proj.config.recording_mode == "mic" else 1
            self.choice_mode.SetSelection(mode_idx)
            self.mode_book.SetSelection(mode_idx)

        self._last_prompt_was_word = None
        self._nonsense_notice_shown = False
        self._rebuild_filter(reset_cursor=True)
        self._load_external_ranges()

        if flagged_only is not None:
            self.btn_record.SetFocus()
        else:
            self.choice_mode.SetFocus()
        self.frame.update_nav_buttons()

    def on_leave(self) -> None:
        self.recorder.abort()
        stop_playback()
        self._sync_record_button()

    def _on_mode_changed(self, _evt: wx.CommandEvent) -> None:
        self.recorder.abort()
        stop_playback()
        idx = self.choice_mode.GetSelection()
        self.mode_book.SetSelection(idx)
        if self.frame.project is not None:
            self.frame.project.config.recording_mode = "mic" if idx == 0 else "external"
            self.frame.project.save()
        self.frame.update_nav_buttons()

    def _load_plan_data(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        inv = json.loads((proj.plan_dir / "inventory.json").read_text(encoding="utf-8"))
        dmap = json.loads((proj.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
        self.vowels = list(inv["vowels"])
        self.lines = list(dmap["lines"])

    def _load_audit_flags(self) -> None:
        proj = self.frame.project
        self.flagged_line_set = set()
        if proj and proj.audit_file.is_file():
            try:
                doc = json.loads(proj.audit_file.read_text(encoding="utf-8"))
                self.flagged_line_set = {int(f["line"]) for f in doc.get("flags", [])}
            except Exception:
                pass

    def _refresh_mic_devices(self) -> None:
        self.devices = list_input_devices()
        self.choice_mic.Clear()
        def_idx = default_input_device_index()
        sel = 0
        for i, d in enumerate(self.devices):
            self.choice_mic.Append(d.label)
            if def_idx is not None and d.index == def_idx:
                sel = i
        if self.devices:
            self.choice_mic.SetSelection(sel)

    def _selected_mic_device(self) -> Optional[int]:
        sel = self.choice_mic.GetSelection()
        if 0 <= sel < len(self.devices):
            return self.devices[sel].index
        return None

    def _take_path(self, line_no: int) -> Path:
        assert self.frame.project is not None
        return self.frame.project.takes_dir / f"line_{line_no:04d}.wav"

    def _is_recorded(self, line_no: int) -> bool:
        p = self._take_path(line_no)
        return p.is_file() and p.stat().st_size > 1000

    def _rebuild_filter(self, *, reset_cursor: bool = False) -> None:
        flt = self.choice_filter.GetSelection()
        self.filtered_indices = []
        recorded_total = 0
        for idx, item in enumerate(self.lines):
            ln = int(item["line"])
            rec = self._is_recorded(ln)
            if rec:
                recorded_total += 1
            if flt == 0:
                self.filtered_indices.append(idx)
            elif flt == 1 and not rec:
                self.filtered_indices.append(idx)
            elif flt == 2 and ln in self.flagged_line_set:
                self.filtered_indices.append(idx)

        self.lbl_progress.SetLabel(
            f"Recorded: {recorded_total} / {len(self.lines)}  (Showing {len(self.filtered_indices)} prompts)"
        )
        self.list_studio.DeleteAllItems()
        for row_i, line_idx in enumerate(self.filtered_indices):
            item = self.lines[line_idx]
            ln = int(item["line"])
            row = self.list_studio.InsertItem(row_i, f"{ln:04d}")
            self.list_studio.SetItem(row, 1, "Real word" if item.get("kind") == "word" else "Nonsense")
            self.list_studio.SetItem(row, 2, str(item["text"]))
            st = "Flagged" if ln in self.flagged_line_set else ("Recorded" if self._is_recorded(ln) else "Pending")
            self.list_studio.SetItem(row, 3, st)

        if reset_cursor:
            # Start on first unrecorded item if in All Prompts view
            self.cursor = 0
            if flt == 0:
                for pos, line_idx in enumerate(self.filtered_indices):
                    if not self._is_recorded(int(self.lines[line_idx]["line"])):
                        self.cursor = pos
                        break
        else:
            self.cursor = max(0, min(self.cursor, max(0, len(self.filtered_indices) - 1)))

        self._show_current_prompt(check_transition=False)
        self.frame.update_nav_buttons()

    def _current_item(self) -> Optional[Dict[str, object]]:
        if not self.filtered_indices or not (0 <= self.cursor < len(self.filtered_indices)):
            return None
        return self.lines[self.filtered_indices[self.cursor]]

    def _remaining_supplements_count(self, from_line_idx: int) -> int:
        return sum(1 for item in self.lines[from_line_idx:] if item.get("kind") != "word")

    def _show_current_prompt(self, *, check_transition: bool = True) -> None:
        item = self._current_item()
        if item is None:
            self.lbl_prompt_header.SetLabel("All prompts in this filter are complete!")
            self.txt_prompt.SetValue("Done!")
            self.lbl_prompt_ipa.SetLabel("Click Next to proceed to Alignment.")
            self.btn_record.Enable(False)
            self.btn_play_ref.Enable(False)
            self.btn_listen.Enable(False)
            self.btn_accept.Enable(False)
            return

        ln = int(item["line"])
        is_word = item.get("kind") == "word"

        # --- Real-word -> Nonsense-supplement transition gate ---
        if check_transition and (not is_word) and (self._last_prompt_was_word is True) and (not self._nonsense_notice_shown):
            self._nonsense_notice_shown = True
            if self.recorder.is_recording:
                self.recorder.abort()
                self._sync_record_button()
            rem = self._remaining_supplements_count(self.filtered_indices[self.cursor])
            wx.MessageBox(
                f"All real words are finished!\n\n"
                f"The remaining {rem} prompt(s) are short nonsense sounds covering rare transitions.\n"
                f"Each nonsense line will automatically play its pronunciation reference — "
                f"listen carefully and repeat the sounds in your own natural voice.\n\n"
                f"Press P anytime to replay the reference.",
                "Nonsense Supplement Lines Ahead",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )

        self._last_prompt_was_word = is_word
        kind_txt = "Real Word" if is_word else "Nonsense Supplement (listen to reference)"
        self.lbl_prompt_header.SetLabel(
            f"Line {ln:04d} of {len(self.lines)}  —  {kind_txt}  ({self.cursor + 1} / {len(self.filtered_indices)} in view)"
        )
        self.txt_prompt.SetValue(str(item["text"]) if is_word else f"[{item['text']}]")
        self.lbl_prompt_ipa.SetLabel("IPA: " + " ".join(item.get("ipa", [])))

        has_take = self._is_recorded(ln)
        self.btn_record.Enable(True)
        self.btn_play_ref.Enable(True)
        self.btn_listen.Enable(has_take)
        self.btn_accept.Enable(has_take)
        if has_take:
            self.txt_verdict.SetValue("Take already recorded. Press L to listen, Space to re-record, or Enter for next.")
        else:
            self.txt_verdict.SetValue(
                "Press Space to start recording, speak naturally, then press Space again to stop."
            )

        if 0 <= self.cursor < self.list_studio.GetItemCount():
            self.list_studio.Select(self.cursor)
            self.list_studio.Focus(self.cursor)
            self.list_studio.SetItemState(
                self.cursor,
                wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
            )
            self.list_studio.EnsureVisible(self.cursor)

        # Auto-play eSpeak reference immediately on every nonsense supplement line!
        if not is_word:
            wx.CallAfter(self._play_current_reference)

    def _sync_record_button(self) -> None:
        if self.recorder.is_recording:
            self.btn_record.SetLabel("&Stop Recording (Space)")
        else:
            self.btn_record.SetLabel("&Start Recording (Space)")

    def _play_current_reference(self) -> None:
        item = self._current_item()
        proj = self.frame.project
        if item is None or proj is None:
            return
        if self.recorder.is_recording:
            self.recorder.abort()
            self._sync_record_button()

        def _play() -> None:
            try:
                play_prompt_reference(item, proj.config.espeak_voice, self.vowels, blocking=False)
            except Exception as exc:
                wx.CallAfter(self.txt_verdict.SetValue, f"Reference playback error: {exc}")

        threading.Thread(target=_play, daemon=True).start()

    def _toggle_recording(self) -> None:
        item = self._current_item()
        if item is None:
            return
        ln = int(item["line"])
        if not self.recorder.is_recording:
            try:
                self.recorder.start(device=self._selected_mic_device())
            except Exception as exc:
                wx.MessageBox(f"Could not open microphone:\n{exc}", "Recording Error", wx.OK | wx.ICON_ERROR, parent=self)
                return
            self._sync_record_button()
            self.txt_verdict.SetValue("RECORDING... Speak the prompt clearly, then press Space to stop.")
            self.frame.announce_status(f"Recording line {ln:04d}...")
        else:
            dest = self._take_path(ln)
            q = self.recorder.stop_and_save(dest, calibrated_noise_db=self.calibrated_noise_db)
            self._sync_record_button()
            self.txt_verdict.SetValue(q.verdict)
            self.btn_listen.Enable(True)
            self.btn_accept.Enable(True)
            if 0 <= self.cursor < self.list_studio.GetItemCount():
                self.list_studio.SetItem(self.cursor, 3, "Recorded" if q.ok else "Check Take")
            rec_cnt = sum(1 for it in self.lines if self._is_recorded(int(it["line"])))
            self.lbl_progress.SetLabel(
                f"Recorded: {rec_cnt} / {len(self.lines)}  (Showing {len(self.filtered_indices)} prompts)"
            )
            self.frame.announce_status(f"Line {ln:04d}: {q.verdict}")
            self.frame.update_nav_buttons()

    def _listen_current_take(self) -> None:
        item = self._current_item()
        if item is None:
            return
        p = self._take_path(int(item["line"]))
        if not p.is_file():
            return
        try:
            play_wav_file(p, blocking=False)
        except Exception as exc:
            self.txt_verdict.SetValue(f"Playback error: {exc}")

    def _accept_and_advance(self) -> None:
        if self.recorder.is_recording:
            self._toggle_recording()
        stop_playback()
        if self.cursor + 1 < len(self.filtered_indices):
            self.cursor += 1
            self._show_current_prompt(check_transition=True)
        else:
            self._rebuild_filter(reset_cursor=False)
            self.frame.announce_status("Reached end of prompt list. Click Next to run MFA alignment.")

    def _move_cursor(self, delta: int) -> None:
        if self.recorder.is_recording:
            self.recorder.abort()
            self._sync_record_button()
        stop_playback()
        if not self.filtered_indices:
            return
        self.cursor = max(0, min(len(self.filtered_indices) - 1, self.cursor + delta))
        self._show_current_prompt(check_transition=delta > 0)

    def _on_studio_list_activate(self, evt: wx.ListEvent) -> None:
        if self.recorder.is_recording:
            self.recorder.abort()
            self._sync_record_button()
        self.cursor = evt.GetIndex()
        self._show_current_prompt(check_transition=True)

    def _on_mic_check(self, _evt: wx.CommandEvent) -> None:
        dev = self._selected_mic_device()

        def _task(prog, _canc):
            prog("Stay quiet for 2.5 seconds to measure room tone...", 0.2)
            return calibrate_room_noise(device=dev, duration_s=2.5)

        def _done(res) -> None:
            noise_db, peak = res
            self.calibrated_noise_db = noise_db
            rating = "Great quiet room!" if noise_db < -45 else ("Usable" if noise_db < -35 else "Noisy room — try reducing background noise")
            msg = f"Room tone: {noise_db:.1f} dBFS (peak {peak:.3f}) — {rating}"
            self.txt_verdict.SetValue(msg)
            self.frame.announce_status(msg)

        self.frame.runner.run("Mic Room-Tone Check", _task, on_success=_done)

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        if self.mode_book.GetSelection() != 0 or self.frame.runner.is_busy:
            evt.Skip()
            return
        focused = wx.Window.FindFocus()
        # Never hijack keys when typing inside an editable text box
        if isinstance(focused, wx.TextCtrl) and focused.IsEditable():
            evt.Skip()
            return

        code = evt.GetKeyCode()
        # Always-available non-conflicting shortcuts: F5 or Ctrl+Space toggles recording
        if code == wx.WXK_F5 or (code == wx.WXK_SPACE and evt.ControlDown()):
            self._toggle_recording()
            return

        if evt.ControlDown() or evt.AltDown() or evt.MetaDown():
            evt.Skip()
            return

        # Never hijack Space from native interactive controls (buttons, checkboxes, choices, lists).
        # Note: when focus is on btn_record (the default), Space naturally presses btn_record!
        native_space_controls = (
            wx.Button,
            wx.CheckBox,
            wx.RadioButton,
            wx.Choice,
            wx.ComboBox,
            wx.ListCtrl,
            wx.SpinCtrl,
        )
        if code == wx.WXK_SPACE:
            if isinstance(focused, native_space_controls):
                evt.Skip()
                return
            self._toggle_recording()
            return

        # Do not hijack bare letter keys from lists or dropdowns (preserves native type-ahead search)
        if isinstance(focused, (wx.ListCtrl, wx.Choice, wx.ComboBox, wx.SpinCtrl)):
            evt.Skip()
            return

        if code in (ord("P"), ord("p")):
            self._play_current_reference()
            return
        if code in (ord("L"), ord("l")):
            self._listen_current_take()
            return
        if code in (ord("R"), ord("r")):
            if self.recorder.is_recording:
                self.recorder.abort()
                self._sync_record_button()
            self.txt_verdict.SetValue("Ready to retake — press Space (or F5) to record.")
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not isinstance(focused, (wx.Button, wx.ListCtrl)):
            if self.btn_accept.IsEnabled():
                self._accept_and_advance()
                return
        evt.Skip()

    # ------------------------------------------------------------------ External Recorder Import handlers
    def _load_external_ranges(self) -> None:
        proj = self.frame.project
        self._ext_ranges = []
        if proj and proj.ranges_file.is_file():
            try:
                self._ext_ranges = read_ranges_file(proj.ranges_file)
            except Exception:
                self._ext_ranges = []
        self._refresh_ranges_list()

    def _refresh_ranges_list(self) -> None:
        self.list_ranges.DeleteAllItems()
        for idx, (wav_name, lo, hi) in enumerate(self._ext_ranges):
            row = self.list_ranges.InsertItem(idx, wav_name)
            self.list_ranges.SetItem(row, 1, str(lo))
            self.list_ranges.SetItem(row, 2, str(hi))
        self._validate_ranges_silent()

    def _validate_ranges_silent(self) -> bool:
        proj = self.frame.project
        if proj is None or not self.lines:
            return False
        if not self._ext_ranges:
            self.txt_range_status.SetValue("No external WAVs added yet. Click 'Add Recording WAV...' to import.")
            return False
        try:
            validate_external_ranges(self._ext_ranges, proj.external_dir, [int(x["line"]) for x in self.lines])
        except Exception as exc:
            self.txt_range_status.SetValue(f"NOT READY: {exc}")
            return False
        self.txt_range_status.SetValue(
            f"VALID: All {len(self.lines)} prompt lines covered once across {len(self._ext_ranges)} WAV file(s) with zero gaps or overlaps."
        )
        return True

    def _on_add_external_wav(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None or not self.lines:
            return
        with wx.FileDialog(
            self,
            "Select External Recording WAV",
            wildcard="WAV audio (*.wav)|*.wav",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            src = Path(dlg.GetPath())

        proj.external_dir.mkdir(parents=True, exist_ok=True)
        dest = proj.external_dir / src.name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)

        # Suggest sensible next line range
        next_lo = (max((hi for _, _, hi in self._ext_ranges), default=0) + 1)
        default_hi = len(self.lines)
        if next_lo > default_hi:
            next_lo = 1
        rng_str = wx.GetTextFromUser(
            f"Enter the prompt line range recorded in {dest.name} (format: first-last, e.g. 1-{default_hi}):",
            "Recording Line Range",
            f"{next_lo}-{default_hi}",
            parent=self,
        ).strip()
        if not rng_str:
            return
        try:
            lo_s, hi_s = rng_str.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        except Exception:
            wx.MessageBox("Use format 'first-last' (for example 1-474).", "Invalid Range", wx.OK | wx.ICON_WARNING, parent=self)
            return
        self._ext_ranges.append((dest.name, lo, hi))
        write_ranges_file(proj.ranges_file, self._ext_ranges)
        self._refresh_ranges_list()
        self.frame.update_nav_buttons()

    def _on_edit_range(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        sel = self.list_ranges.GetFirstSelected()
        if proj is None or sel < 0:
            return
        wav_name, lo, hi = self._ext_ranges[sel]
        rng_str = wx.GetTextFromUser(
            f"Edit line range for {wav_name} (first-last):",
            "Edit Line Range",
            f"{lo}-{hi}",
            parent=self,
        ).strip()
        if not rng_str:
            return
        try:
            lo_s, hi_s = rng_str.split("-", 1)
            self._ext_ranges[sel] = (wav_name, int(lo_s), int(hi_s))
        except Exception:
            wx.MessageBox("Use format 'first-last'.", "Invalid Range", wx.OK | wx.ICON_WARNING, parent=self)
            return
        write_ranges_file(proj.ranges_file, self._ext_ranges)
        self._refresh_ranges_list()
        self.frame.update_nav_buttons()

    def _on_remove_range(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        sel = self.list_ranges.GetFirstSelected()
        if proj is None or sel < 0:
            return
        self._ext_ranges.pop(sel)
        write_ranges_file(proj.ranges_file, self._ext_ranges)
        self._refresh_ranges_list()
        self.frame.update_nav_buttons()

    def _on_validate_ranges(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        write_ranges_file(proj.ranges_file, self._ext_ranges)
        ok = self._validate_ranges_silent()
        self.frame.update_nav_buttons()
        if ok:
            proj.set_stage("record", "done")
            wx.MessageBox("All prompt lines are validly mapped with zero gaps or overlaps!", "Ranges Valid", wx.OK | wx.ICON_INFORMATION, parent=self)

    def _on_open_plan_folder(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj:
            wx.LaunchDefaultApplication(str(proj.plan_dir))

    def _on_export_refs(self, *, only_supplements: bool) -> None:
        proj = self.frame.project
        if proj is None:
            return
        out_dir = proj.plan_dir / ("ref_supplements" if only_supplements else "ref_all")

        def _task(prog, _canc):
            prog("Rendering eSpeak pronunciation reference WAVs...", None)
            written = export_reference_wavs(proj, out_dir, only_supplements=only_supplements)
            return len(written), out_dir

        def _done(res) -> None:
            n, d = res
            wx.MessageBox(f"Exported {n} reference WAV file(s) to:\n{d}", "Reference Audio Exported", wx.OK | wx.ICON_INFORMATION, parent=self)

        self.frame.runner.run("Export Reference Audio", _task, on_success=_done)

    def can_proceed(self) -> bool:
        proj = self.frame.project
        if proj is None or not self.lines:
            return False
        if self.mode_book.GetSelection() == 0:
            # Allow proceeding once all prompts (or existing segments) are ready
            return all(self._is_recorded(int(it["line"])) for it in self.lines) or proj.segments_csv.is_file()
        return self._validate_ranges_silent() or proj.segments_csv.is_file()
