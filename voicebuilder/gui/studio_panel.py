"""Screen 2 — Dual-Mode Audio Studio (Built-in PC Mic Studio + External Recorder Import).

Built-in Mic Studio UX invariants:
  - No read-only prompt edit box and no automatic focus jumping (keeps NVDA silent during recording).
  - Auto-speaks every prompt with eSpeak (default ON) on Start Recording (in headphone mode),
    Accept (A), Retake (R), Previous, Skip, and Prompt List jumps.
  - Bare single-key workflow from any Studio button/checkbox:
      * Space (on Start/Stop Recording) / F5 / Ctrl+Space: Start or Stop recording.
      * A: Accept take & advance (keeps mic rolling in Continuous headphone mode).
           If a take clips, is too quiet, too short, or has severe low SNR, stays on the
           same prompt, plays a brief descending warning tone, and replays the word.
      * R: Retake (clears buffer & keeps rolling in Continuous mode; replays the word).
      * P: Replay eSpeak pronunciation reference on demand.
      * L: Listen back to the saved take.
  - Real-words -> Nonsense-supplements transition gate:
      * Force-stops any active recording cleanly.
      * Alerts the speaker via an accessible dialog that the remaining prompts are
        nonsense sounds and they should listen carefully.
      * Auto-plays the eSpeak audio reference on every nonsense supplement line.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import wx

from ..aligner import (
    ExternalWavInspection,
    inspect_external_wav,
    read_ranges_file,
    validate_external_ranges,
    write_ranges_file,
)
from ..recorder import (
    TakeRecorder,
    analyze_take,
    calibrate_room_noise,
    default_input_device_index,
    list_input_devices,
    play_warning_tone,
    play_wav_file,
    save_take_wav,
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
        self._last_warned_take: Optional[Tuple[int, object, object]] = None  # (line_no, audio, quality)
        self._ext_ranges: List[Tuple[str, int, int]] = []
        self._ext_inspections: Dict[Tuple[str, int, int, int, int], ExternalWavInspection] = {}
        self._ext_check_gen: int = 0

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

        # Filter, headphone mode, auto-speak & progress row
        flt_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_flt = wx.StaticText(p, label="Show Pr&ompts:")
        self.choice_filter = wx.Choice(p, choices=["All Prompts", "Unrecorded Only", "Flagged from Audit"])
        self.choice_filter.SetName("Prompt Filter")
        self.choice_filter.SetSelection(0)
        self.choice_filter.Bind(wx.EVT_CHOICE, lambda _e: self._rebuild_filter(reset_cursor=True))
        self.chk_continuous = wx.CheckBox(p, label="Contin&uous recording (headphones)")
        self.chk_continuous.SetName("Continuous recording with headphones")
        self.chk_continuous.SetToolTip(
            "Keep the microphone rolling across prompts when pressing A (accept) or R (retake) "
            "while listening to eSpeak prompts on headphones."
        )
        self.chk_auto_speak = wx.CheckBox(p, label="Auto-sp&eak prompts with eSpeak")
        self.chk_auto_speak.SetName("Auto-speak prompts with eSpeak")
        self.chk_auto_speak.SetValue(True)
        self.chk_auto_speak.SetToolTip(
            "Automatically speak each prompt with eSpeak on Start Recording (continuous mode), "
            "Accept (A), Retake (R), Previous, and Skip without needing to press P."
        )
        self.lbl_progress = wx.StaticText(p, label="Recorded: 0 / 0")
        flt_row.Add(lbl_flt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        flt_row.Add(self.choice_filter, 0, wx.RIGHT, 12)
        flt_row.Add(self.chk_continuous, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        flt_row.Add(self.chk_auto_speak, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        flt_row.Add(self.lbl_progress, 1, wx.ALIGN_CENTER_VERTICAL)
        s.Add(flt_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Current prompt display box (non-focusable visual text so NVDA stays silent while recording)
        prompt_box = wx.StaticBox(p, label="Current Prompt")
        ps = wx.StaticBoxSizer(prompt_box, wx.VERTICAL)

        self.lbl_prompt_header = wx.StaticText(p, label="Line 0 / 0")
        self.lbl_prompt_header.SetFont(self.lbl_prompt_header.GetFont().Bold())
        ps.Add(self.lbl_prompt_header, 0, wx.ALL, 6)

        self.lbl_prompt_word = wx.StaticText(p, label="", style=wx.ALIGN_CENTRE_HORIZONTAL | wx.ST_NO_AUTORESIZE)
        self.lbl_prompt_word.SetFont(self.lbl_prompt_word.GetFont().Bold().Scaled(2.0))
        self.lbl_prompt_word.SetMinSize((-1, 52))
        ps.Add(self.lbl_prompt_word, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.lbl_prompt_ipa = wx.StaticText(p, label="")
        ps.Add(self.lbl_prompt_ipa, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Action buttons
        act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_record = wx.Button(p, label="&Start Recording (Space)")
        self.btn_accept = wx.Button(p, label="&Accept && Next (A)")
        self.btn_retake = wx.Button(p, label="&Retake (R)")
        self.btn_play_ref = wx.Button(p, label="&Play Reference (P)")
        self.btn_listen = wx.Button(p, label="&Listen Take (L)")
        self.btn_prev = wx.Button(p, label="Pre&vious")
        self.btn_skip = wx.Button(p, label="S&kip")

        self.btn_record.Bind(wx.EVT_BUTTON, lambda _e: self._toggle_recording())
        self.btn_accept.Bind(wx.EVT_BUTTON, lambda _e: self._accept_and_advance())
        self.btn_retake.Bind(wx.EVT_BUTTON, lambda _e: self._retake_current())
        self.btn_play_ref.Bind(wx.EVT_BUTTON, lambda _e: self._play_current_reference())
        self.btn_listen.Bind(wx.EVT_BUTTON, lambda _e: self._listen_current_take())
        self.btn_prev.Bind(wx.EVT_BUTTON, lambda _e: self._move_cursor(-1))
        self.btn_skip.Bind(wx.EVT_BUTTON, lambda _e: self._move_cursor(+1))

        for b in (
            self.btn_record,
            self.btn_accept,
            self.btn_retake,
            self.btn_play_ref,
            self.btn_listen,
            self.btn_prev,
            self.btn_skip,
        ):
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
        btn_exp_refs = wx.Button(p, label="Export Re&ference Audio...")
        btn_open_script.Bind(wx.EVT_BUTTON, self._on_open_plan_folder)
        btn_exp_refs.Bind(wx.EVT_BUTTON, self._on_export_refs_dialog)
        es.Add(btn_open_script, 0, wx.ALL, 6)
        es.Add(btn_exp_refs, 0, wx.ALL, 6)
        s.Add(es, 0, wx.EXPAND | wx.BOTTOM, 8)

        imp_box = wx.StaticBox(p, label="2. Import External Recording WAV(s) & Strict Line Ranges (auto-saved & validated)")
        ims = wx.StaticBoxSizer(imp_box, wx.VERTICAL)

        self.list_ranges = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_ranges.SetName("External Recording Range Table")
        self.list_ranges.InsertColumn(0, "WAV File (in recordings/external/)", width=220)
        self.list_ranges.InsertColumn(1, "First Line", width=75)
        self.list_ranges.InsertColumn(2, "Last Line", width=75)
        self.list_ranges.InsertColumn(3, "Expected", width=75)
        self.list_ranges.InsertColumn(4, "Spoken Check", width=260)
        self.list_ranges.SetMinSize((-1, 140))
        self.list_ranges.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit_range)
        ims.Add(self.list_ranges, 1, wx.EXPAND | wx.ALL, 6)

        rbtns = wx.BoxSizer(wx.HORIZONTAL)
        btn_add_wav = wx.Button(p, label="&Add Recording WAV...")
        btn_edit_rng = wx.Button(p, label="&Edit Selected Range...")
        btn_del_rng = wx.Button(p, label="&Remove")
        btn_recheck = wx.Button(p, label="Re-check A&udio")
        btn_add_wav.Bind(wx.EVT_BUTTON, self._on_add_external_wav)
        btn_edit_rng.Bind(wx.EVT_BUTTON, self._on_edit_range)
        btn_del_rng.Bind(wx.EVT_BUTTON, self._on_remove_range)
        btn_recheck.Bind(wx.EVT_BUTTON, self._on_recheck_external_audio)
        for b in (btn_add_wav, btn_edit_rng, btn_del_rng, btn_recheck):
            rbtns.Add(b, 0, wx.RIGHT, 6)
        ims.Add(rbtns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.txt_range_status = wx.TextCtrl(p, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 72))
        self.txt_range_status.SetName("External Range Validation Status")
        ims.Add(self.txt_range_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        s.Add(ims, 1, wx.EXPAND)

        p.SetSizer(s)
        return p

    # ------------------------------------------------------------------ Lifecycle & helpers
    def on_enter(
        self,
        *,
        flagged_only: Optional[Sequence[int]] = None,
        unrecorded_only: bool = False,
    ) -> None:
        proj = self.frame.project
        if proj is None:
            return
        self._load_plan_data()
        self._refresh_mic_devices()
        self._load_audit_flags()
        self.calibrated_noise_db = getattr(proj.config, "calibrated_noise_db", None)
        self._last_warned_take = None

        if flagged_only is not None:
            self.flagged_line_set = {int(x) for x in flagged_only}
            proj.config.recording_mode = "mic"
            proj.save()
            self.choice_mode.SetSelection(0)
            self.mode_book.SetSelection(0)
            self._sync_filter_choices(select_key="flagged")
        elif unrecorded_only:
            proj.config.recording_mode = "mic"
            proj.save()
            self.choice_mode.SetSelection(0)
            self.mode_book.SetSelection(0)
            self._sync_filter_choices(select_key="unrecorded")
        else:
            mode_idx = 0 if proj.config.recording_mode == "mic" else 1
            self.choice_mode.SetSelection(mode_idx)
            self.mode_book.SetSelection(mode_idx)
            self._sync_filter_choices(select_key="all")

        self._last_prompt_was_word = None
        self._nonsense_notice_shown = False
        self._rebuild_filter(reset_cursor=True)
        self._load_external_ranges()

        if flagged_only is not None or unrecorded_only:
            self.btn_record.SetFocus()
        else:
            self.choice_mode.SetFocus()
        self.frame.update_nav_buttons()

    def _sync_filter_choices(self, *, select_key: Optional[str] = None) -> None:
        cur_key = select_key or self._current_filter_key()
        total = len(self.lines)
        rec = sum(1 for it in self.lines if self._is_recorded(int(it["line"])))
        unrec = max(0, total - rec)
        keys = ["all", "unrecorded"]
        labels = [f"All Prompts ({total})", f"Unrecorded Only ({unrec})"]
        if self.flagged_line_set or cur_key == "flagged":
            keys.append("flagged")
            labels.append(f"Flagged from Audit ({len(self.flagged_line_set)})")
        self._filter_keys = keys
        self.choice_filter.Clear()
        for lbl in labels:
            self.choice_filter.Append(lbl)
        sel = keys.index(cur_key) if cur_key in keys else 0
        self.choice_filter.SetSelection(sel)

    def _current_filter_key(self) -> str:
        keys = getattr(self, "_filter_keys", ["all", "unrecorded", "flagged"])
        sel = self.choice_filter.GetSelection()
        if 0 <= sel < len(keys):
            return keys[sel]
        return "all"

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
        flt = self._current_filter_key()
        self.filtered_indices = []
        recorded_total = 0
        for idx, item in enumerate(self.lines):
            ln = int(item["line"])
            rec = self._is_recorded(ln)
            if rec:
                recorded_total += 1
            if flt == "all":
                self.filtered_indices.append(idx)
            elif flt == "unrecorded" and not rec:
                self.filtered_indices.append(idx)
            elif flt == "flagged" and ln in self.flagged_line_set:
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
            if flt == "all":
                for pos, line_idx in enumerate(self.filtered_indices):
                    if not self._is_recorded(int(self.lines[line_idx]["line"])):
                        self.cursor = pos
                        break
        else:
            self.cursor = max(0, min(self.cursor, max(0, len(self.filtered_indices) - 1)))

        self._show_current_prompt(check_transition=False, auto_speak=False)
        self.frame.update_nav_buttons()

    def _current_item(self) -> Optional[Dict[str, object]]:
        if not self.filtered_indices or not (0 <= self.cursor < len(self.filtered_indices)):
            return None
        return self.lines[self.filtered_indices[self.cursor]]

    def _remaining_supplements_count(self, from_line_idx: int) -> int:
        return sum(1 for item in self.lines[from_line_idx:] if item.get("kind") != "word")

    def _show_current_prompt(self, *, check_transition: bool = True, auto_speak: bool = True) -> None:
        item = self._current_item()
        if item is None:
            self.lbl_prompt_header.SetLabel("All prompts in this filter are complete!")
            self.lbl_prompt_word.SetLabel("Done!")
            self.lbl_prompt_ipa.SetLabel("Click Next to proceed to Alignment.")
            self.btn_record.Enable(False)
            self.btn_retake.Enable(False)
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
                f"Press Space to resume recording, A to accept, R to retake, or P to replay the reference.",
                "Nonsense Supplement Lines Ahead",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )

        self._last_prompt_was_word = is_word
        kind_txt = "Real Word" if is_word else "Nonsense Supplement (listen to reference)"
        self.lbl_prompt_header.SetLabel(
            f"Line {ln:04d} of {len(self.lines)}  —  {kind_txt}  ({self.cursor + 1} / {len(self.filtered_indices)} in view)"
        )
        self.lbl_prompt_word.SetLabel(str(item["text"]) if is_word else f"[{item['text']}]")
        self.lbl_prompt_ipa.SetLabel("IPA: " + " ".join(item.get("ipa", [])))

        has_take = self._is_recorded(ln)
        self.btn_record.Enable(True)
        self.btn_retake.Enable(True)
        self.btn_play_ref.Enable(True)
        self.btn_listen.Enable(has_take)
        self.btn_accept.Enable(has_take or self.recorder.is_recording)
        if self.recorder.is_recording:
            self.txt_verdict.SetValue(
                "RECORDING (continuous)... Speak the prompt, then press A to accept && advance, R to retake, or Space to stop."
            )
        elif has_take:
            self.txt_verdict.SetValue("Take already recorded. Press L to listen, Space to re-record, R to retake, or A for next.")
        else:
            self.txt_verdict.SetValue(
                "Press Space on Start Recording, speak clearly, then press A to accept && advance (R to retake, P to replay)."
            )

        if 0 <= self.cursor < self.list_studio.GetItemCount():
            self.list_studio.Select(self.cursor)
            if wx.Window.FindFocus() is self.list_studio:
                self.list_studio.Focus(self.cursor)
            self.list_studio.EnsureVisible(self.cursor)

        # Auto-speak current prompt with eSpeak (for all words when Auto-speak is checked,
        # and always for nonsense supplements). Only skip during non-continuous recording
        # to avoid speaker bleed.
        should_speak = auto_speak and (self.chk_auto_speak.GetValue() or (not is_word))
        if should_speak and (self.chk_continuous.GetValue() or not self.recorder.is_recording):
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
        # In Continuous headphone mode, keep the microphone rolling while reference plays in headphones.
        # In standard (speaker) mode, stop recording so speaker audio does not bleed into the take.
        if self.recorder.is_recording and not self.chk_continuous.GetValue():
            self.recorder.abort()
            self._sync_record_button()

        def _play() -> None:
            try:
                play_prompt_reference(item, proj.config.espeak_voice, self.vowels, blocking=False)
            except Exception as exc:
                wx.CallAfter(self.txt_verdict.SetValue, f"Reference playback error: {exc}")

        threading.Thread(target=_play, daemon=True).start()

    def _play_retry_cue_and_reference(self, item: Dict[str, object], *, speak_again: bool) -> None:
        """Play a brief descending warning tone, then optionally re-speak the same prompt."""
        proj = self.frame.project
        if proj is None:
            return
        voice = proj.config.espeak_voice
        vowels = list(self.vowels)

        def _worker() -> None:
            try:
                play_warning_tone(blocking=True)
                if speak_again:
                    play_prompt_reference(item, voice, vowels, blocking=False)
            except Exception as exc:
                wx.CallAfter(self.txt_verdict.SetValue, f"Reference playback error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_save_feedback(self, ln: int, q) -> None:
        self._last_warned_take = None
        self.txt_verdict.SetValue(q.verdict)
        self.btn_listen.Enable(True)
        self.btn_accept.Enable(True)
        if 0 <= self.cursor < self.list_studio.GetItemCount():
            self.list_studio.SetItem(self.cursor, 3, "Recorded" if q.ok else "Warning")
        rec_cnt = sum(1 for it in self.lines if self._is_recorded(int(it["line"])))
        self.lbl_progress.SetLabel(
            f"Recorded: {rec_cnt} / {len(self.lines)}  (Showing {len(self.filtered_indices)} prompts)"
        )
        self._sync_filter_choices()
        proj = self.frame.project
        if proj is not None:
            new_stage = "done" if (self.lines and rec_cnt >= len(self.lines)) else ("partial" if rec_cnt > 0 else "pending")
            if proj.config.stages.get("record") != new_stage:
                proj.set_stage("record", new_stage)
        self.frame.announce_status(f"Line {ln:04d}: {q.verdict}")
        self.frame.update_nav_buttons()

    def _toggle_recording(self) -> None:
        item = self._current_item()
        if item is None:
            return
        ln = int(item["line"])
        continuous = self.chk_continuous.GetValue()
        if not self.recorder.is_recording:
            try:
                self.recorder.start(device=self._selected_mic_device(), stop_audio=not continuous)
            except Exception as exc:
                wx.MessageBox(f"Could not open microphone:\n{exc}", "Recording Error", wx.OK | wx.ICON_ERROR, parent=self)
                return
            self._sync_record_button()
            self.btn_accept.Enable(True)
            if continuous:
                self.txt_verdict.SetValue(
                    "RECORDING (continuous)... Speak clearly, press A to accept && advance, R to retake, Space to stop."
                )
                if self.chk_auto_speak.GetValue() or item.get("kind") != "word":
                    wx.CallAfter(self._play_current_reference)
            else:
                self.txt_verdict.SetValue(
                    "RECORDING... Speak clearly, then press A to accept && advance or Space to stop."
                )
            self.frame.announce_status(f"Recording line {ln:04d}...")
        else:
            dest = self._take_path(ln)
            q = self.recorder.stop_and_save(dest, calibrated_noise_db=self.calibrated_noise_db)
            self._sync_record_button()
            self._finish_save_feedback(ln, q)

    def _retake_current(self) -> None:
        item = self._current_item()
        if item is None:
            return
        continuous = self.chk_continuous.GetValue()
        want_speak = self.chk_auto_speak.GetValue() or (item.get("kind") != "word")
        if self.recorder.is_recording:
            if continuous:
                self.recorder.clear_buffer()
                self.txt_verdict.SetValue(
                    "Retaking (still recording) — listen and speak the prompt again, then press A."
                )
                if want_speak:
                    wx.CallAfter(self._play_current_reference)
                return
            self.recorder.abort()
            self._sync_record_button()
        self.txt_verdict.SetValue("Ready to retake — press Space to record.")
        if want_speak:
            wx.CallAfter(self._play_current_reference)

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

    def _accept_and_advance(self, *, force: bool = False) -> None:
        item = self._current_item()
        if item is None:
            return
        ln = int(item["line"])
        continuous = self.chk_continuous.GetValue()
        has_next = (self.cursor + 1) < len(self.filtered_indices)

        # Shift+A (force=True) after a warning saves the previously warned take immediately
        if force and self._last_warned_take is not None and self._last_warned_take[0] == ln:
            _, prev_audio, prev_q = self._last_warned_take
            self._last_warned_take = None
            if self.recorder.is_recording:
                if continuous:
                    self.recorder.clear_buffer()
                else:
                    self.recorder.abort()
                    self._sync_record_button()
            if continuous and not has_next and self.recorder.is_recording:
                self.recorder.abort()
                self._sync_record_button()
            save_take_wav(prev_audio, self._take_path(ln), self.recorder.sample_rate)
            self._finish_save_feedback(ln, prev_q)
        elif self.recorder.is_recording:
            dest = self._take_path(ln)
            if continuous:
                # Extract & clear buffer while keeping the mic rolling in case a retry is needed
                audio = self.recorder.flush_buffer()
            else:
                audio = self.recorder.stop_buffer()
                self._sync_record_button()

            q = analyze_take(audio, self.recorder.sample_rate, calibrated_noise_db=self.calibrated_noise_db)
            if (not force) and q.needs_warning:
                # Warn only on genuine problems (very quiet/silent, insanely short, excessive silence, or hard clip)
                self._last_warned_take = (ln, audio, q)
                self.txt_verdict.SetValue(
                    f"{q.verdict}  — Speak again && press A to retry, or press Shift+A to keep it anyway."
                )
                if 0 <= self.cursor < self.list_studio.GetItemCount():
                    self.list_studio.SetItem(self.cursor, 3, "Warning")
                self.frame.announce_status(f"Line {ln:04d} warning: {q.verdict}")
                speak_again = (
                    (self.chk_auto_speak.GetValue() or (item.get("kind") != "word"))
                    and (continuous or not self.recorder.is_recording)
                )
                self._play_retry_cue_and_reference(item, speak_again=speak_again)
                return

            if continuous and not has_next:
                self.recorder.abort()
                self._sync_record_button()

            save_take_wav(audio, dest, self.recorder.sample_rate)
            self._finish_save_feedback(ln, q)
        else:
            if not self._is_recorded(ln):
                return

        if not (continuous and self.recorder.is_recording):
            stop_playback()
        if has_next:
            self.cursor += 1
            self._show_current_prompt(check_transition=True, auto_speak=True)
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
        self._show_current_prompt(check_transition=delta > 0, auto_speak=True)

    def _on_studio_list_activate(self, evt: wx.ListEvent) -> None:
        if self.recorder.is_recording:
            self.recorder.abort()
            self._sync_record_button()
        self.cursor = evt.GetIndex()
        self._show_current_prompt(check_transition=True, auto_speak=True)

    def _on_mic_check(self, _evt: wx.CommandEvent) -> None:
        dev = self._selected_mic_device()

        def _task(prog, _canc):
            prog("Stay quiet for 2.5 seconds to measure room tone...", 0.2)
            return calibrate_room_noise(device=dev, duration_s=2.5)

        def _done(res) -> None:
            noise_db, peak = res
            self.calibrated_noise_db = noise_db
            if self.frame.project is not None:
                self.frame.project.config.calibrated_noise_db = noise_db
                self.frame.project.save()
            rating = "Great quiet room!" if noise_db < -45 else ("Usable" if noise_db < -35 else "Noisy room — try reducing background noise")
            msg = f"Room tone: {noise_db:.1f} dBFS (peak {peak:.3f}) — {rating} (saved)"
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
        # When focus is on btn_record, Space naturally starts/stops recording!
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

        if code in (ord("A"), ord("a")):
            self._accept_and_advance(force=evt.ShiftDown())
            return
        if code in (ord("R"), ord("r")):
            self._retake_current()
            return
        if code in (ord("P"), ord("p")):
            self._play_current_reference()
            return
        if code in (ord("L"), ord("l")):
            self._listen_current_take()
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

    def _ext_cache_key(self, wav_name: str, lo: int, hi: int) -> Optional[Tuple[str, int, int, int, int]]:
        proj = self.frame.project
        if proj is None:
            return None
        p = proj.external_dir / wav_name
        if not p.is_file():
            return None
        st = p.stat()
        return (wav_name, lo, hi, int(st.st_size), int(st.st_mtime))

    def _refresh_ranges_list(self, *, run_audio_check: bool = True) -> None:
        self.list_ranges.DeleteAllItems()
        need_check = False
        for idx, (wav_name, lo, hi) in enumerate(self._ext_ranges):
            row = self.list_ranges.InsertItem(idx, wav_name)
            self.list_ranges.SetItem(row, 1, str(lo))
            self.list_ranges.SetItem(row, 2, str(hi))
            exp_n = max(0, hi - lo + 1)
            self.list_ranges.SetItem(row, 3, str(exp_n))
            ck = self._ext_cache_key(wav_name, lo, hi)
            if ck is not None and ck in self._ext_inspections:
                self.list_ranges.SetItem(row, 4, self._ext_inspections[ck].status_label)
            elif ck is None:
                self.list_ranges.SetItem(row, 4, "Missing WAV file")
            else:
                self.list_ranges.SetItem(row, 4, "Checking audio..." if run_audio_check else "Not checked")
                need_check = True
        self._validate_ranges_silent()
        if run_audio_check and need_check:
            self._start_external_audio_check()

    def _on_recheck_external_audio(self, _evt: wx.CommandEvent) -> None:
        self._ext_inspections.clear()
        self._refresh_ranges_list(run_audio_check=True)

    def _start_external_audio_check(self) -> None:
        proj = self.frame.project
        if proj is None or not self._ext_ranges:
            return
        self._ext_check_gen += 1
        gen = self._ext_check_gen
        ranges_snap = list(self._ext_ranges)
        ext_dir = proj.external_dir
        phone_counts = {int(it["line"]): len(it.get("phones", [])) for it in self.lines}
        line_texts = {int(it["line"]): str(it.get("text", "")) for it in self.lines}

        def _worker() -> None:
            results: List[Tuple[Tuple[str, int, int, int, int], ExternalWavInspection]] = []
            for wav_name, lo, hi in ranges_snap:
                if gen != self._ext_check_gen:
                    return
                p = ext_dir / wav_name
                if not p.is_file() or lo > hi:
                    continue
                st = p.stat()
                ck = (wav_name, lo, hi, int(st.st_size), int(st.st_mtime))
                if ck in self._ext_inspections:
                    continue
                try:
                    insp = inspect_external_wav(
                        p,
                        list(range(lo, hi + 1)),
                        line_phone_counts=phone_counts,
                        line_texts=line_texts,
                    )
                    results.append((ck, insp))
                except Exception:
                    continue

            def _apply() -> None:
                if not self or gen != self._ext_check_gen:
                    return
                for ck, insp in results:
                    self._ext_inspections[ck] = insp
                self._refresh_ranges_list(run_audio_check=False)

            wx.CallAfter(_apply)

        threading.Thread(target=_worker, daemon=True).start()

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

        # Summarize spoken-utterance pre-flight results if available
        warnings: List[str] = []
        pending_cnt = 0
        for wav_name, lo, hi in self._ext_ranges:
            ck = self._ext_cache_key(wav_name, lo, hi)
            insp = self._ext_inspections.get(ck) if ck is not None else None
            if insp is None:
                pending_cnt += 1
            elif not insp.direct_vad_ok:
                detail = f" {insp.shift_warning}" if insp.shift_warning else ""
                warnings.append(
                    f"{wav_name}: {insp.detected_count}/{insp.expected_count} spoken utterances ({insp.delta:+d}).{detail}"
                )

        if warnings:
            self.txt_range_status.SetValue(
                f"RANGE VALID — SPOKEN COUNT WARNING ({len(warnings)} file(s)):\n"
                + "\n".join(warnings)
                + "\nCheck your WAV/range or proceed to Step 3 for chunked Pass-1 alignment."
            )
        elif pending_cnt > 0:
            self.txt_range_status.SetValue(
                f"VALID: All {len(self.lines)} prompt lines covered across {len(self._ext_ranges)} WAV file(s). "
                "Checking spoken word counts..."
            )
        else:
            self.txt_range_status.SetValue(
                f"VALID: All {len(self.lines)} prompt lines covered across {len(self._ext_ranges)} WAV file(s). "
                "Spoken word counts match 100% — ready for fast, drift-free alignment!"
            )
        if proj.config.stages.get("record") != "done":
            proj.set_stage("record", "done")
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

    def _on_open_plan_folder(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj:
            wx.LaunchDefaultApplication(str(proj.plan_dir))

    def _on_export_refs_dialog(self, _evt: wx.CommandEvent) -> None:
        has_sups = any(it.get("kind") != "word" for it in self.lines)
        if not has_sups:
            self._on_export_refs(only_supplements=False)
            return
        with wx.SingleChoiceDialog(
            self,
            "Which eSpeak pronunciation reference WAVs would you like to export?",
            "Export Reference Audio",
            [
                "Nonsense supplement prompts only (recommended)",
                "All prompts (real words + nonsense supplements)",
            ],
        ) as dlg:
            dlg.SetSelection(0)
            if dlg.ShowModal() == wx.ID_OK:
                self._on_export_refs(only_supplements=(dlg.GetSelection() == 0))

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
            # Allow proceeding once at least 1 mic take (or existing alignment) is ready
            return any(self._is_recorded(int(it["line"])) for it in self.lines) or proj.segments_csv.is_file()
        return self._validate_ranges_silent() or proj.segments_csv.is_file()
