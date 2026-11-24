"""Screen 3 — MFA Forced Alignment, Flagged-Lines Audit Queue & Partial Pickup Patching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

import wx

from ..aligner import (
    AlignmentReport,
    align_external_recordings,
    align_mic_takes,
    apply_pickup_recording,
    export_pickup_script,
    find_unaligned_mic_lines,
    parse_line_list,
)
from ..downloader import get_cached_mfa_model_path
from ..paths import find_mfa_env
from ..planner import ExtensionSummary, extend_project_plan
from ..recorder import play_wav_file
from ..reference import export_reference_wavs

if TYPE_CHECKING:
    from .app import WizardFrame


def _format_line_ranges(lines: List[int]) -> str:
    """Format an ordered list of line numbers compactly (e.g. [475..674] -> '475-674')."""
    if not lines:
        return ""
    parts: List[str] = []
    start = prev = lines[0]
    for ln in lines[1:]:
        if ln == prev + 1:
            prev = ln
        else:
            parts.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = ln
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ", ".join(parts)


class AlignPanel(wx.Panel):
    """Step 3: Run MFA alignment, inspect flagged lines, and patch via mic retakes or external pickups."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame
        self.flags: List[Dict[str, object]] = []

        root = wx.BoxSizer(wx.VERTICAL)

        self.lbl_status = wx.StaticText(self, label="")
        root.Add(self.lbl_status, 0, wx.BOTTOM, 8)

        # Alignment trigger row
        run_box = wx.StaticBox(self, label="Run Montreal Forced Aligner")
        rs = wx.StaticBoxSizer(run_box, wx.HORIZONTAL)
        self.btn_align = wx.Button(self, label="&Run MFA Alignment")
        self.btn_align_flagged = wx.Button(self, label="Align New / &Flagged Mic Takes Only")
        self.btn_align.Bind(wx.EVT_BUTTON, self._on_run_align)
        self.btn_align_flagged.Bind(wx.EVT_BUTTON, self._on_realign_flagged_mic)
        rs.Add(self.btn_align, 0, wx.ALL, 6)
        rs.Add(self.btn_align_flagged, 0, wx.ALL, 6)
        root.Add(rs, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Audit summary + flagged queue
        aud_box = wx.StaticBox(self, label="Audit Queue (Flagged Lines Needing Attention)")
        aus = wx.StaticBoxSizer(aud_box, wx.VERTICAL)

        self.txt_audit_summary = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 56))
        self.txt_audit_summary.SetName("Alignment Audit Summary")
        aus.Add(self.txt_audit_summary, 0, wx.EXPAND | wx.ALL, 6)

        self.list_flags = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_flags.SetName("Flagged Lines Queue")
        self.list_flags.InsertColumn(0, "Line", width=65)
        self.list_flags.InsertColumn(1, "Prompt", width=150)
        self.list_flags.InsertColumn(2, "Source WAV", width=180)
        self.list_flags.InsertColumn(3, "Reasons", width=310)
        self.list_flags.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_play_clip)
        aus.Add(self.list_flags, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Action row for retaking / patching ONLY flagged lines or adding extension words
        act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_play_clip = wx.Button(self, label="&Play Selected Clip")
        self.btn_retake_mic = wx.Button(self, label="Retake Flagged in &Mic Studio")
        self.btn_exp_pickup = wx.Button(self, label="&Export Pickup Script (Flagged)")
        self.btn_extend = wx.Button(self, label="E&xtend Corpus (+Extra Words)...")
        self.btn_imp_pickup = wx.Button(self, label="&Import && Apply Pickup / Extension WAV...")

        self.btn_play_clip.Bind(wx.EVT_BUTTON, self._on_play_clip)
        self.btn_retake_mic.Bind(wx.EVT_BUTTON, self._on_retake_in_studio)
        self.btn_exp_pickup.Bind(wx.EVT_BUTTON, self._on_export_pickup)
        self.btn_extend.Bind(wx.EVT_BUTTON, self._on_extend_corpus)
        self.btn_imp_pickup.Bind(wx.EVT_BUTTON, self._on_import_pickup)

        for b in (self.btn_play_clip, self.btn_retake_mic, self.btn_exp_pickup, self.btn_extend, self.btn_imp_pickup):
            act_row.Add(b, 0, wx.RIGHT, 6)
        aus.Add(act_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        tip = wx.StaticText(
            self,
            label=(
                "Partial pickups & corpus extensions: export flagged lines OR click 'Extend Corpus (+Extra Words)...' "
                "to append extra words for more diphone takes, record ONLY those new lines, then click "
                "'Import & Apply Pickup / Extension WAV...' to align and merge them without re-aligning earlier recordings."
            ),
        )
        tip.Wrap(720)
        aus.Add(tip, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        root.Add(aus, 1, wx.EXPAND)
        self.SetSizer(root)

    def on_enter(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        mfa_ok = find_mfa_env() is not None
        mdl_ok = get_cached_mfa_model_path(proj.config.mfa_model) is not None
        mfa_txt = "MFA ready" if mfa_ok else "MFA will auto-install (~2.4 GB)"
        mdl_txt = f"model '{proj.config.mfa_model}' cached" if mdl_ok else f"model '{proj.config.mfa_model}' will auto-download (~15-60 MB)"
        self.lbl_status.SetLabel(
            f"Mode: {proj.config.recording_mode}  |  {mfa_txt}  |  {mdl_txt}"
        )
        self._refresh_audit()
        self.btn_align.SetFocus()

    def _pending_batch(self) -> Dict[str, object] | None:
        proj = self.frame.project
        if proj is None:
            return None
        for pb in reversed(proj.config.pickups):
            if not pb.get("applied") and pb.get("lines"):
                return pb
        return None

    def _refresh_audit(self) -> None:
        proj = self.frame.project
        self.list_flags.DeleteAllItems()
        self.flags = []
        has_plan = bool(proj and (proj.plan_dir / "diphone_map.json").is_file())
        self.btn_extend.Enable(has_plan)
        if proj is None or not proj.audit_file.is_file():
            self.txt_audit_summary.SetValue("Alignment has not been run yet. Click 'Run MFA Alignment' to align and audit your recordings.")
            self.btn_align_flagged.Enable(False)
            self.btn_play_clip.Enable(False)
            self.btn_retake_mic.Enable(False)
            self.btn_exp_pickup.Enable(False)
            self.btn_imp_pickup.Enable(False)
            self.frame.update_nav_buttons()
            return

        doc = json.loads(proj.audit_file.read_text(encoding="utf-8"))
        self.flags = list(doc.get("flags", []))
        summary_txt = (
            f"Aligned: {doc.get('aligned_lines', 0)} / {doc.get('total_lines', 0)} lines "
            f"({doc.get('phone_ok_lines', 0)} exact phone counts)  |  Flagged: {len(self.flags)}"
        )
        pending = self._pending_batch()
        if pending is not None:
            p_lines = [int(x) for x in pending.get("lines", [])]
            summary_txt += (
                f"\nPending batch '{pending.get('id')}': {len(p_lines)} line(s) ({_format_line_ranges(p_lines)}) "
                "— click 'Import & Apply Pickup / Extension WAV...' once recorded."
            )
        self.txt_audit_summary.SetValue(summary_txt)
        for idx, f in enumerate(self.flags):
            row = self.list_flags.InsertItem(idx, f"{int(f['line']):04d}")
            self.list_flags.SetItem(row, 1, str(f.get("text", "")))
            self.list_flags.SetItem(row, 2, str(f.get("source_wav", "")))
            self.list_flags.SetItem(row, 3, ", ".join(f.get("reasons", [])))

        has_flags = len(self.flags) > 0
        unaligned_mic = find_unaligned_mic_lines(proj)
        self.btn_align_flagged.Enable(bool(unaligned_mic) or (has_flags and proj.config.recording_mode == "mic"))
        self.btn_play_clip.Enable(has_flags)
        self.btn_retake_mic.Enable(has_flags)
        self.btn_exp_pickup.Enable(has_flags)
        # Allow importing a pickup or extension WAV anytime after initial alignment
        self.btn_imp_pickup.Enable(proj.segments_csv.is_file())
        self.frame.update_nav_buttons()

    def _handle_report(self, rep: AlignmentReport) -> None:
        self._refresh_audit()
        approx_note = ""
        if rep.phone_map_approximate:
            approx_note = f" ({len(rep.phone_map_approximate)} phone(s) used IPA fallback mapping)"
        msg = (
            f"Alignment complete: {rep.aligned_lines}/{rep.total_lines} lines aligned, "
            f"{len(rep.flags)} flagged{approx_note}."
        )
        self.frame.announce_status(msg)
        if not rep.flags:
            wx.MessageBox(
                f"All {rep.aligned_lines} lines aligned cleanly with zero flags!\nClick Next to build your MBROLA voice.",
                "Alignment Clean",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )

    def _on_run_align(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        if find_mfa_env() is None:
            res = wx.MessageBox(
                "Montreal Forced Aligner is not installed yet.\n"
                "Clicking Yes will automatically download micromamba (~10 MB) and install MFA (~2.4 GB) "
                "into the app's cache folder before aligning.\n\nProceed?",
                "On-Demand MFA Setup",
                wx.YES_NO | wx.ICON_QUESTION,
                parent=self,
            )
            if res != wx.YES:
                return

        def _task(prog, canc) -> AlignmentReport:
            if proj.config.recording_mode == "external":
                return align_external_recordings(proj, progress=prog, cancel=canc)
            return align_mic_takes(proj, progress=prog, cancel=canc)

        self.frame.runner.run("MFA Alignment", _task, on_success=self._handle_report)

    def _on_realign_flagged_mic(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        flagged_lines = [int(f["line"]) for f in self.flags]
        new_mic_lines = find_unaligned_mic_lines(proj)
        line_nums = sorted(set(flagged_lines) | set(new_mic_lines))
        if not line_nums:
            return
        self.frame.runner.run(
            f"Align {len(line_nums)} New / Flagged Mic Take(s)",
            lambda prog, canc: align_mic_takes(proj, only_lines=line_nums, progress=prog, cancel=canc),
            on_success=self._handle_report,
        )

    def _on_play_clip(self, _evt: wx.CommandEvent) -> None:
        sel = self.list_flags.GetFirstSelected()
        if sel < 0 or sel >= len(self.flags):
            return
        clip = Path(str(self.flags[sel].get("clip_wav", "")))
        if not clip.is_file():
            wx.MessageBox("Clip file does not exist for this line yet.", "No Clip", wx.OK | wx.ICON_INFORMATION, parent=self)
            return
        play_wav_file(clip, blocking=False)

    def _on_retake_in_studio(self, _evt: wx.CommandEvent) -> None:
        if not self.flags:
            return
        line_nums = [int(f["line"]) for f in self.flags]
        self.frame.go_to_step(2, flagged_only=line_nums)

    def _on_export_pickup(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None or not self.flags:
            return
        line_nums = [int(f["line"]) for f in self.flags]
        bid, script_p, _ = export_pickup_script(proj, line_nums)

        want_ref = wx.MessageBox(
            f"Exported pickup script ({len(line_nums)} line(s)) to:\n{script_p}\n\n"
            "Would you also like to export eSpeak pronunciation reference WAVs for those lines?",
            f"Pickup Script {bid} Exported",
            wx.YES_NO | wx.ICON_QUESTION,
            parent=self,
        )
        if want_ref == wx.YES:
            ref_dir = proj.pickups_dir / f"{bid}_ref"
            self.frame.runner.run(
                f"Export {bid} Reference Audio",
                lambda prog, _canc: export_reference_wavs(proj, ref_dir, lines=line_nums),
                on_success=lambda written: wx.MessageBox(
                    f"Exported {len(written)} reference WAV(s) to:\n{ref_dir}",
                    "Pickup Reference Exported",
                    wx.OK | wx.ICON_INFORMATION,
                    parent=self,
                ),
            )

    def _on_extend_corpus(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        raw = wx.GetTextFromUser(
            "How many extra real words would you like to append for additional diphone takes?\n"
            "(Your existing prompts, recordings, and alignments stay untouched.)",
            "Extend Corpus (+Extra Words)",
            "200",
            parent=self,
        ).strip()
        if not raw:
            return
        try:
            extra_words = int(raw)
            if not (10 <= extra_words <= 1000):
                raise ValueError("Enter a number between 10 and 1000.")
        except Exception as exc:
            wx.MessageBox(f"Invalid word count:\n{exc}", "Extend Corpus", wx.OK | wx.ICON_WARNING, parent=self)
            return

        def _done(summary: ExtensionSummary) -> None:
            self._refresh_audit()
            self.frame.announce_status(
                f"Added {summary.added_words} extra words (lines {summary.first_line}-{summary.last_line})."
            )
            wx.MessageBox(
                f"Appended {summary.added_words} new real words (lines {summary.first_line}-{summary.last_line}, "
                f"targeting {summary.target_takes}+ takes/pair) without changing your earlier lines.\n"
                f"Diphones with 2+ takes: {summary.diphones_2plus_before} -> {summary.diphones_2plus_after}.\n\n"
                f"Standalone recording script saved to:\n{summary.script_path}\n\n"
                "Next steps:\n"
                "- External recorder: record ONLY those new words, then click 'Import & Apply Pickup / Extension WAV...' "
                "(lines will be pre-filled automatically).\n"
                "- PC Microphone Studio: go Back to Step 2, switch 'Show Prompts' to 'Unrecorded Only' to record the new words, "
                "then return here and click 'Align New / Flagged Mic Takes Only'.",
                f"Corpus Extended ({summary.batch_id})",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )

        self.frame.runner.run(
            f"Extend Corpus (+{extra_words} words)",
            lambda prog, canc: extend_project_plan(
                proj,
                extra_words=extra_words,
                min_takes=2,
                catalog=self.frame.catalog,
                progress=prog,
                cancel=canc,
            ),
            on_success=_done,
        )

    def _on_import_pickup(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        with wx.FileDialog(
            self,
            "Select External Pickup or Extension Recording WAV",
            wildcard="WAV audio (*.wav)|*.wav",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            wav_path = Path(dlg.GetPath())

        pending = self._pending_batch()
        if pending is not None:
            default_lines = _format_line_ranges([int(x) for x in pending.get("lines", [])])
        else:
            default_lines = _format_line_ranges([int(f["line"]) for f in self.flags])
        spec = wx.GetTextFromUser(
            f"Enter the ordered line numbers recorded in {wav_path.name}\n"
            "(comma-separated or ranges, e.g. 475-674 or 142, 319, 410-412):",
            "Pickup / Extension Line Numbers",
            default_lines,
            parent=self,
        ).strip()
        if not spec:
            return
        try:
            line_nums = parse_line_list(spec)
        except Exception as exc:
            wx.MessageBox(f"Invalid line list:\n{exc}", "Invalid Line List", wx.OK | wx.ICON_WARNING, parent=self)
            return

        self.frame.runner.run(
            f"Apply Pickup / Extension ({len(line_nums)} line(s))",
            lambda prog, canc: apply_pickup_recording(proj, wav_path, line_nums, progress=prog, cancel=canc),
            on_success=self._handle_report,
        )

    def can_proceed(self) -> bool:
        proj = self.frame.project
        return bool(proj and proj.segments_csv.is_file() and proj.audit_file.is_file())
