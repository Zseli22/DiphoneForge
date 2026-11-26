"""Screen 3 — MFA Forced Alignment, Flagged-Lines Audit Queue & Workflow-Aware Patching."""

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
    inspect_external_ranges,
    inspect_external_wav,
    parse_line_list,
    re_audit_project,
    read_ranges_file,
)
from ..downloader import get_cached_mfa_model_path
from ..paths import find_mfa_env
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
    """Step 3: Smart MFA alignment, flagged-lines audit queue, and workflow-appropriate fixes."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame
        self.flags: List[Dict[str, object]] = []

        root = wx.BoxSizer(wx.VERTICAL)

        self.lbl_status = wx.StaticText(self, label="")
        root.Add(self.lbl_status, 0, wx.BOTTOM, 8)

        # Smart single alignment trigger (+ secondary Force Re-Align All when already aligned in mic mode)
        run_box = wx.StaticBox(self, label="Run Montreal Forced Aligner")
        rs = wx.StaticBoxSizer(run_box, wx.HORIZONTAL)
        self.btn_align = wx.Button(self, label="&Align Recordings")
        self.btn_realign_all = wx.Button(self, label="Re-Align A&ll")
        self.btn_align.Bind(wx.EVT_BUTTON, self._on_smart_align)
        self.btn_realign_all.Bind(wx.EVT_BUTTON, self._on_realign_all)
        rs.Add(self.btn_align, 0, wx.ALL, 6)
        rs.Add(self.btn_realign_all, 0, wx.ALL, 6)
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
        self.list_flags.InsertColumn(3, "Reasons", width=320)
        self.list_flags.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_play_clip)
        aus.Add(self.list_flags, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Workflow-adaptive action row
        act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_play_clip = wx.Button(self, label="&Play Selected Clip")
        self.btn_dismiss = wx.Button(self, label="&Dismiss Flag")
        self.btn_exclude = wx.Button(self, label="E&xclude Take")
        self.btn_retake_mic = wx.Button(self, label="Retake Flagged in &Mic Studio")
        self.btn_exp_pickup = wx.Button(self, label="&Export Pickup Script")
        self.btn_imp_pickup = wx.Button(self, label="&Import Pickup / Extension WAV...")

        self.btn_play_clip.Bind(wx.EVT_BUTTON, self._on_play_clip)
        self.btn_dismiss.Bind(wx.EVT_BUTTON, self._on_dismiss_flag)
        self.btn_exclude.Bind(wx.EVT_BUTTON, self._on_exclude_take)
        self.btn_retake_mic.Bind(wx.EVT_BUTTON, self._on_retake_in_studio)
        self.btn_exp_pickup.Bind(wx.EVT_BUTTON, self._on_export_pickup)
        self.btn_imp_pickup.Bind(wx.EVT_BUTTON, self._on_import_pickup)

        for b in (
            self.btn_play_clip,
            self.btn_dismiss,
            self.btn_exclude,
            self.btn_retake_mic,
            self.btn_exp_pickup,
            self.btn_imp_pickup,
        ):
            act_row.Add(b, 0, wx.RIGHT, 6)
        aus.Add(act_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.lbl_tip = wx.StaticText(self, label="")
        aus.Add(self.lbl_tip, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        root.Add(aus, 1, wx.EXPAND)
        self.SetSizer(root)

    def on_enter(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        mfa_ok = find_mfa_env() is not None
        mdl_ok = get_cached_mfa_model_path(proj.config.mfa_model) is not None
        mfa_txt = "MFA ready" if mfa_ok else "MFA will auto-install (~415 MB download, ~2.4 GB installed)"
        mdl_txt = f"model '{proj.config.mfa_model}' cached" if mdl_ok else f"model '{proj.config.mfa_model}' will auto-download (~15-60 MB)"
        mode_lbl = "PC Microphone Studio" if proj.config.recording_mode == "mic" else "External Recorder Import"
        self.lbl_status.SetLabel(f"Workflow: {mode_lbl}  |  {mfa_txt}  |  {mdl_txt}")
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

    def _incremental_mic_targets(self) -> List[int]:
        proj = self.frame.project
        if proj is None or proj.config.recording_mode != "mic" or not proj.segments_csv.is_file():
            return []
        unaligned = set(find_unaligned_mic_lines(proj))
        flagged_present = {
            int(f["line"])
            for f in self.flags
            if (proj.takes_dir / f"line_{int(f['line']):04d}.wav").is_file()
        }
        return sorted(unaligned | flagged_present)

    def _refresh_audit(self) -> None:
        proj = self.frame.project
        self.list_flags.DeleteAllItems()
        self.flags = []
        is_mic = bool(proj and proj.config.recording_mode == "mic")

        # Adapt buttons to the active recording workflow
        self.btn_retake_mic.Show(is_mic)
        self.btn_exp_pickup.Show(not is_mic)
        self.btn_imp_pickup.Show(not is_mic)

        if is_mic:
            self.lbl_tip.SetLabel(
                "Tip: Press Enter to listen to a flagged clip, Dismiss a false positive, Exclude a bad take, "
                "or retake flagged lines in Mic Studio and click Align again (only changed lines are re-aligned)."
            )
        else:
            self.lbl_tip.SetLabel(
                "Tip: Export a pickup script for flagged lines, record ONLY those lines on your external recorder, "
                "then click 'Import Pickup / Extension WAV...' to patch them without re-aligning earlier files."
            )
        self.lbl_tip.Wrap(740)

        if proj is None or not proj.audit_file.is_file():
            self.btn_align.SetLabel("&Align Recordings")
            self.btn_realign_all.Show(False)
            self.txt_audit_summary.SetValue(
                "Alignment has not been run yet. Click 'Align Recordings' to run forced alignment and audit your takes."
            )
            for b in (self.btn_play_clip, self.btn_dismiss, self.btn_exclude, self.btn_retake_mic, self.btn_exp_pickup, self.btn_imp_pickup):
                b.Enable(False)
            self.Layout()
            self.frame.update_nav_buttons()
            return

        doc = json.loads(proj.audit_file.read_text(encoding="utf-8"))
        self.flags = list(doc.get("flags", []))
        excl_cnt = len(doc.get("excluded_lines", []))
        excl_note = f"  |  Excluded: {excl_cnt}" if excl_cnt else ""
        summary_txt = (
            f"Aligned: {doc.get('aligned_lines', 0)} / {doc.get('total_lines', 0)} lines "
            f"({doc.get('phone_ok_lines', 0)} exact phone counts)  |  Flagged: {len(self.flags)}{excl_note}"
        )
        warns = [str(w) for w in doc.get("warnings", []) if w]
        if warns:
            summary_txt += "\nWARNING: " + " | ".join(warns)
        pending = self._pending_batch()
        if pending is not None:
            p_lines = [int(x) for x in pending.get("lines", [])]
            if is_mic:
                summary_txt += (
                    f"\nPending reinforcement batch '{pending.get('id')}': {len(p_lines)} line(s) ({_format_line_ranges(p_lines)}) "
                    "— record them in Step 2 (Unrecorded Only), then click Align."
                )
            else:
                summary_txt += (
                    f"\nPending batch '{pending.get('id')}': {len(p_lines)} line(s) ({_format_line_ranges(p_lines)}) "
                    "— click 'Import Pickup / Extension WAV...' once recorded."
                )
        self.txt_audit_summary.SetValue(summary_txt)

        for idx, f in enumerate(self.flags):
            row = self.list_flags.InsertItem(idx, f"{int(f['line']):04d}")
            self.list_flags.SetItem(row, 1, str(f.get("text", "")))
            self.list_flags.SetItem(row, 2, str(f.get("source_wav", "")))
            self.list_flags.SetItem(row, 3, ", ".join(f.get("reasons", [])))

        # Smart Align button label
        inc_targets = self._incremental_mic_targets()
        if is_mic and proj.segments_csv.is_file() and inc_targets:
            self.btn_align.SetLabel(f"&Align {len(inc_targets)} New / Changed Take(s)")
            self.btn_realign_all.Show(True)
            self.btn_realign_all.Enable(True)
        elif is_mic and proj.segments_csv.is_file():
            self.btn_align.SetLabel("&Align Recordings (Up to Date)")
            self.btn_realign_all.Show(True)
            self.btn_realign_all.Enable(True)
        else:
            self.btn_align.SetLabel("&Align Recordings")
            self.btn_realign_all.Show(False)

        has_flags = len(self.flags) > 0
        self.btn_play_clip.Enable(has_flags)
        self.btn_dismiss.Enable(has_flags)
        self.btn_exclude.Enable(has_flags)
        self.btn_retake_mic.Enable(has_flags)
        self.btn_exp_pickup.Enable(has_flags)
        self.btn_imp_pickup.Enable(proj.segments_csv.is_file())
        self.Layout()
        self.frame.update_nav_buttons()

    def _confirm_mfa_if_needed(self) -> bool:
        if find_mfa_env() is not None:
            return True
        res = wx.MessageBox(
            "Montreal Forced Aligner is not installed yet.\n"
            "Clicking Yes will automatically download micromamba (~10 MB) and install MFA "
            "(~415 MB download, ~2.4 GB installed) into the app's cache folder before aligning.\n\nProceed?",
            "On-Demand MFA Setup",
            wx.YES_NO | wx.ICON_QUESTION,
            parent=self,
        )
        return res == wx.YES

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
        if rep.warnings:
            wx.MessageBox(
                "Alignment finished with warning(s):\n\n- "
                + "\n- ".join(rep.warnings)
                + f"\n\nFlagged lines: {len(rep.flags)}. Review the Audit Queue below.",
                "Alignment Warnings",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
        elif not rep.flags:
            wx.MessageBox(
                f"All {rep.aligned_lines} aligned lines passed audit with zero active flags!\n"
                "Click Next to build your MBROLA voice.",
                "Alignment Clean",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )

    def _confirm_external_preflight(self) -> bool:
        proj = self.frame.project
        if proj is None or not proj.ranges_file.is_file():
            return True
        try:
            ranges = read_ranges_file(proj.ranges_file)
            inspections = inspect_external_ranges(proj, ranges)
        except Exception:
            return True
        issues = [ins for ins in inspections if not ins.direct_vad_ok]
        if not issues:
            return True
        lines_msg: List[str] = []
        for ins in issues:
            detail = f"\n    {ins.shift_warning}" if ins.shift_warning else ""
            lines_msg.append(
                f"- {ins.wav_name}: detected {ins.detected_count} spoken utterance(s) "
                f"for {ins.expected_count} expected line(s) ({ins.delta:+d}).{detail}"
            )
        res = wx.MessageBox(
            "Spoken word count mismatch or shift detected before alignment:\n\n"
            + "\n".join(lines_msg)
            + "\n\nYes: Continue using pause-condensed chunked Pass-1 alignment (skipped/extra lines will be isolated and flagged).\n"
            "No: Stop so you can verify your WAV files or line ranges in Step 2 first.",
            "External Recording Pre-Flight Warning",
            wx.YES_NO | wx.ICON_WARNING,
            parent=self,
        )
        return res == wx.YES

    def _on_smart_align(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None or not self._confirm_mfa_if_needed():
            return
        if proj.config.recording_mode == "external":
            if not self._confirm_external_preflight():
                return
            self.frame.runner.run(
                "MFA Alignment (External Recordings)",
                lambda prog, canc: align_external_recordings(proj, progress=prog, cancel=canc),
                on_success=self._handle_report,
            )
            return

        inc_targets = self._incremental_mic_targets()
        if proj.segments_csv.is_file() and inc_targets:
            self.frame.runner.run(
                f"Align {len(inc_targets)} New / Changed Mic Take(s)",
                lambda prog, canc: align_mic_takes(proj, only_lines=inc_targets, progress=prog, cancel=canc),
                on_success=self._handle_report,
            )
            return
        self.frame.runner.run(
            "MFA Alignment (All Mic Takes)",
            lambda prog, canc: align_mic_takes(proj, progress=prog, cancel=canc),
            on_success=self._handle_report,
        )

    def _on_realign_all(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None or not self._confirm_mfa_if_needed():
            return
        self.frame.runner.run(
            "Re-Align All Mic Takes",
            lambda prog, canc: align_mic_takes(proj, progress=prog, cancel=canc),
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

    def _on_dismiss_flag(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        sel = self.list_flags.GetFirstSelected()
        if proj is None or sel < 0 or sel >= len(self.flags):
            return
        ln = int(self.flags[sel]["line"])
        if ln not in proj.config.dismissed_flags:
            proj.config.dismissed_flags.append(ln)
            proj.save()
        re_audit_project(proj)
        self._refresh_audit()
        self.frame.announce_status(f"Dismissed audit flag for line {ln:04d}.")

    def _on_exclude_take(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        sel = self.list_flags.GetFirstSelected()
        if proj is None or sel < 0 or sel >= len(self.flags):
            return
        ln = int(self.flags[sel]["line"])
        txt = str(self.flags[sel].get("text", ""))
        if ln not in proj.config.excluded_lines:
            proj.config.excluded_lines.append(ln)
            proj.save()
        re_audit_project(proj)
        self._refresh_audit()
        self.frame.announce_status(f"Excluded line {ln:04d} ({txt}) from voice build.")

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

        # Pre-flight spoken utterance count check on pickup WAV
        try:
            dmap = json.loads((proj.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
            p_counts = {int(it["line"]): len(it.get("phones", [])) for it in dmap.get("lines", [])}
            l_texts = {int(it["line"]): str(it.get("text", "")) for it in dmap.get("lines", [])}
            insp = inspect_external_wav(wav_path, line_nums, line_phone_counts=p_counts, line_texts=l_texts)
            if not insp.direct_vad_ok:
                detail = f"\n{insp.shift_warning}" if insp.shift_warning else ""
                res = wx.MessageBox(
                    f"Pre-flight check on {wav_path.name}:\n"
                    f"Detected {insp.detected_count} spoken utterance(s) for {insp.expected_count} line(s) ({insp.delta:+d}).{detail}\n\n"
                    "Continue applying this pickup/extension recording?",
                    "Pickup Audio Count Warning",
                    wx.YES_NO | wx.ICON_WARNING,
                    parent=self,
                )
                if res != wx.YES:
                    return
        except Exception:
            pass

        self.frame.runner.run(
            f"Apply Pickup / Extension ({len(line_nums)} line(s))",
            lambda prog, canc: apply_pickup_recording(proj, wav_path, line_nums, progress=prog, cancel=canc),
            on_success=self._handle_report,
        )

    def can_proceed(self) -> bool:
        proj = self.frame.project
        return bool(proj and proj.segments_csv.is_file() and proj.audit_file.is_file())
