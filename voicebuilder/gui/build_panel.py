"""Screen 4 — Build MBROLA Database, Live Speech Preview Sandbox, Reinforcement Pass & NVDA Install."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import wx

from ..builder import _db_name_for_rate, build_voice_databases, refresh_unit_grid
from ..planner import ReinforcementPreview, apply_reinforcement_pass, compute_reinforcement_pass
from ..preview import export_voice_package, install_voice_to_nvda, synthesize_preview
from ..recorder import play_wav_file, stop_playback

if TYPE_CHECKING:
    from .app import WizardFrame


class BuildPanel(wx.Panel):
    """Step 4: Build validated MBROLA DB(s), audition live speech, reinforce low-take pairs, and export/install."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame

        root = wx.BoxSizer(wx.VERTICAL)

        # 1. Build & Upgrade box
        bld_box = wx.StaticBox(self, label="1. Build, Refine & Upgrade Voice Bank")
        bs = wx.StaticBoxSizer(bld_box, wx.VERTICAL)

        rate_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_rates = wx.StaticText(self, label="Target Sample &Rates:")
        self.chk_rate_22k = wx.CheckBox(self, label="22.05 kHz (higher clarity)")
        self.chk_rate_16k = wx.CheckBox(self, label="16 kHz (smaller && faster)")
        self.chk_rate_22k.SetName("Build 22.05 kHz voice")
        self.chk_rate_16k.SetName("Build 16 kHz voice")
        self.chk_rate_22k.Bind(wx.EVT_CHECKBOX, self._on_rates_changed)
        self.chk_rate_16k.Bind(wx.EVT_CHECKBOX, self._on_rates_changed)
        rate_row.Add(lbl_rates, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        rate_row.Add(self.chk_rate_22k, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        rate_row.Add(self.chk_rate_16k, 0, wx.ALIGN_CENTER_VERTICAL)
        bs.Add(rate_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        b_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_build = wx.Button(self, label="Buil&d MBROLA Voice Bank")
        self.btn_picker = wx.Button(self, label="Interactive &Take Picker...")
        self.btn_reinforce = wx.Button(self, label="Add Reinforce&ment Pass (Low-Take Pairs)...")
        self.btn_build.Bind(wx.EVT_BUTTON, self._on_build)
        self.btn_picker.Bind(wx.EVT_BUTTON, lambda _e: self._on_open_picker())
        self.btn_reinforce.Bind(wx.EVT_BUTTON, self._on_reinforce_pass)
        b_row.Add(self.btn_build, 0, wx.RIGHT, 8)
        b_row.Add(self.btn_picker, 0, wx.RIGHT, 8)
        b_row.Add(self.btn_reinforce, 0)
        bs.Add(b_row, 0, wx.ALL, 6)

        self.txt_summary = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 86))
        self.txt_summary.SetName("Voice Build Summary")
        bs.Add(self.txt_summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        root.Add(bs, 0, wx.EXPAND | wx.BOTTOM, 8)

        # 2. Live listening sandbox
        prev_box = wx.StaticBox(self, label="2. Live Listening Sandbox (32-bit eSpeak NG + MBROLA Host)")
        ps = wx.StaticBoxSizer(prev_box, wx.VERTICAL)

        vrow = wx.BoxSizer(wx.HORIZONTAL)
        lbl_v = wx.StaticText(self, label="Built &Voice:")
        self.choice_voice = wx.Choice(self)
        self.choice_voice.SetName("Built Voice Variant")
        vrow.Add(lbl_v, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        vrow.Add(self.choice_voice, 0)
        ps.Add(vrow, 0, wx.ALL, 6)

        lbl_txt = wx.StaticText(self, label="Test Te&xt to Synthesize:")
        self.txt_preview = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 74))
        self.txt_preview.SetName("Test Text to Synthesize")
        ps.Add(lbl_txt, 0, wx.LEFT | wx.RIGHT, 6)
        ps.Add(self.txt_preview, 1, wx.EXPAND | wx.ALL, 6)

        pbtns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_speak = wx.Button(self, label="&Speak Preview")
        self.btn_stop = wx.Button(self, label="St&op Playback")
        self.btn_inspect_preview = wx.Button(self, label="Inspect Text in Take &Picker...")
        self.btn_save_wav = wx.Button(self, label="Save Preview &WAV...")
        self.btn_speak.Bind(wx.EVT_BUTTON, self._on_speak)
        self.btn_stop.Bind(wx.EVT_BUTTON, lambda _e: stop_playback())
        self.btn_inspect_preview.Bind(wx.EVT_BUTTON, self._on_inspect_preview)
        self.btn_save_wav.Bind(wx.EVT_BUTTON, self._on_save_wav)
        for b in (self.btn_speak, self.btn_stop, self.btn_inspect_preview, self.btn_save_wav):
            pbtns.Add(b, 0, wx.RIGHT, 6)
        ps.Add(pbtns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        root.Add(ps, 1, wx.EXPAND | wx.BOTTOM, 8)

        # 3. Export / Install
        exp_box = wx.StaticBox(self, label="3. Export or Install into NVDA")
        es = wx.StaticBoxSizer(exp_box, wx.HORIZONTAL)
        self.btn_export = wx.Button(self, label="&Export Standalone Voice Package...")
        self.btn_install_nvda = wx.Button(self, label="&Install into NVDA Add-on")
        self.btn_export.Bind(wx.EVT_BUTTON, self._on_export)
        self.btn_install_nvda.Bind(wx.EVT_BUTTON, self._on_install_nvda)
        es.Add(self.btn_export, 0, wx.ALL, 6)
        es.Add(self.btn_install_nvda, 0, wx.ALL, 6)
        root.Add(es, 0, wx.EXPAND)

        self.SetSizer(root)

    def on_enter(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        rates = list(proj.config.rates or [22050])
        self.chk_rate_22k.SetValue(22050 in rates)
        self.chk_rate_16k.SetValue(16000 in rates)
        spec = proj.language_spec(self.frame.catalog)
        if not self.txt_preview.GetValue().strip():
            self.txt_preview.SetValue(spec.preview_text or "Hello world. This is a test of my new MBROLA voice.")
        self._refresh_built_voices()
        # Smart initial focus: Build if unbuilt, Speak Preview once built
        if self._built_db_names():
            self.btn_speak.SetFocus()
        else:
            self.btn_build.SetFocus()

    def on_leave(self) -> None:
        stop_playback()

    def _selected_rates(self) -> List[int]:
        rates: List[int] = []
        if self.chk_rate_22k.GetValue():
            rates.append(22050)
        if self.chk_rate_16k.GetValue():
            rates.append(16000)
        return rates

    def _on_rates_changed(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        rates = self._selected_rates()
        if rates:
            proj.config.rates = rates
            proj.save()

    def _built_db_names(self) -> List[str]:
        proj = self.frame.project
        if proj is None:
            return []
        candidates: List[str] = []
        active_rates = list(proj.config.rates or [22050])
        for rate in active_rates:
            n = _db_name_for_rate(proj.config.voice_id, rate, active_rates)
            if n not in candidates:
                candidates.append(n)
        for rate in (22050, 16000):
            n = _db_name_for_rate(proj.config.voice_id, rate, [22050, 16000])
            if n not in candidates:
                candidates.append(n)
        if proj.config.voice_id not in candidates:
            candidates.append(proj.config.voice_id)
        return [n for n in candidates if (proj.build_dir / n / n).is_file()]

    def _refresh_built_voices(self) -> None:
        proj = self.frame.project
        names = self._built_db_names()
        self.choice_voice.Clear()
        for n in names:
            self.choice_voice.Append(n)
        if names:
            self.choice_voice.SetSelection(0)

        has_built = bool(names)
        has_grid_src = bool(proj and (proj.segments_csv.is_file() or (proj.build_dir / "unit_grid.csv").is_file()))
        for b in (self.btn_speak, self.btn_save_wav, self.btn_export, self.btn_install_nvda):
            b.Enable(has_built)
        self.btn_picker.Enable(has_grid_src)
        self.btn_inspect_preview.Enable(has_grid_src)
        self.btn_reinforce.Enable(bool(proj and (proj.plan_dir / "diphone_map.json").is_file()))

        if not has_built or proj is None:
            self.txt_summary.SetValue("No voice built yet. Click 'Build MBROLA Voice Bank' to analyze, pack, and validate.")
            return

        lines = []
        for n in names:
            sp = proj.build_dir / f"build_summary_{n}.json"
            if sp.is_file():
                s = json.loads(sp.read_text(encoding="utf-8"))
                f0 = s.get("speaker_f0", {}).get("median_hz", "?")
                lines.append(
                    f"{n} ({s.get('rate')} Hz): {s.get('db_mb')} MB | Speaker F0={f0} Hz | "
                    f"Units={s.get('units')} ({s.get('natural')} recorded + {s.get('edge')} edges + {s.get('fallback')} fills) | "
                    f"Failures={s.get('failures')}, Clipped={s.get('validation_clipped')}"
                )
        self.txt_summary.SetValue("\n".join(lines))

    def _selected_db_name(self) -> str:
        sel = self.choice_voice.GetSelection()
        if sel == wx.NOT_FOUND:
            raise ValueError("No built voice selected.")
        return self.choice_voice.GetString(sel)

    def _on_build(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        rates = self._selected_rates()
        if not rates:
            wx.MessageBox(
                "Select at least one target sample rate (22.05 kHz or 16 kHz) before building.",
                "Sample Rate Required",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
            self.chk_rate_22k.SetFocus()
            return
        proj.config.rates = rates
        proj.save()
        self.frame.runner.run(
            "Build MBROLA Voice Bank",
            lambda prog, canc: build_voice_databases(proj, progress=prog, cancel=canc),
            on_success=lambda _res: (
                self._refresh_built_voices(),
                self.frame.announce_status("Voice build complete! Use 'Speak Preview' to listen to your new voice."),
            ),
        )

    def _needs_unit_grid_refresh(self) -> bool:
        proj = self.frame.project
        if proj is None or not proj.segments_csv.is_file():
            return False
        ug = proj.build_dir / "unit_grid.csv"
        if not ug.is_file():
            return True
        ug_mtime = ug.stat().st_mtime
        if proj.segments_csv.stat().st_mtime > ug_mtime:
            return True
        if proj.audit_file.is_file() and proj.audit_file.stat().st_mtime > ug_mtime:
            return True
        return False

    def _launch_picker_dialog(self, *, initial_phrase: Optional[str] = None) -> None:
        proj = self.frame.project
        if proj is None:
            return
        ug = proj.build_dir / "unit_grid.csv"
        if not ug.is_file():
            wx.MessageBox("Align your recordings first to generate diphone takes.", "Take Picker", wx.OK | wx.ICON_INFORMATION, parent=self)
            return
        from .take_picker import TakePickerDialog

        with TakePickerDialog(
            self,
            unit_grid_csv=ug,
            segments_csv=proj.segments_csv,
            audio_dir=proj.recordings_dir,
            overrides_json=proj.build_dir / "take_overrides.json",
            voice=proj.config.espeak_voice,
            inventory_json=proj.plan_dir / "inventory.json",
            diphone_map_json=proj.plan_dir / "diphone_map.json",
            initial_phrase=initial_phrase,
        ) as dlg:
            dlg.CenterOnParent()
            dlg.ShowModal()
            if dlg.changed:
                self.frame.announce_status("Take overrides saved. Click 'Build MBROLA Voice Bank' to rebuild with your pinned takes.")

    def _on_open_picker(self, *, initial_phrase: Optional[str] = None) -> None:
        proj = self.frame.project
        if proj is None:
            return
        if self._needs_unit_grid_refresh():
            self.frame.runner.run(
                "Refresh Diphone Takes",
                lambda prog, _canc: refresh_unit_grid(proj, progress=prog),
                on_success=lambda _ug: self._launch_picker_dialog(initial_phrase=initial_phrase),
            )
            return
        self._launch_picker_dialog(initial_phrase=initial_phrase)

    def _on_inspect_preview(self, _evt: wx.CommandEvent) -> None:
        phrase = self.txt_preview.GetValue().strip()
        self._on_open_picker(initial_phrase=phrase if phrase else None)

    def _on_reinforce_pass(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return

        def _on_preview_ready(preview: ReinforcementPreview) -> None:
            n_words = len(preview.new_words)
            msg = (
                f"Reinforcement pass calculated (targeting {preview.target_takes}+ takes for lowest-take core pairs):\n\n"
                f"  • New words to append: {n_words} (approx. {preview.est_minutes} min recording)\n"
                f"  • Single-take pairs: {preview.single_take_before} -> {preview.single_take_after}\n"
                f"  • Pairs with 2+ takes: {preview.diphones_2plus_before} -> {preview.diphones_2plus_after}\n\n"
                "Your existing prompts, recordings, and alignments will stay completely untouched.\n"
                "Append these words to your project now?"
            )
            res = wx.MessageBox(msg, "Add Reinforcement Pass", wx.YES_NO | wx.ICON_QUESTION, parent=self)
            if res != wx.YES:
                return
            summary = apply_reinforcement_pass(proj, preview)
            self.frame.announce_status(
                f"Added {summary.added_words} reinforcement words (lines {summary.first_line}-{summary.last_line})."
            )
            if proj.config.recording_mode == "mic":
                go_rec = wx.MessageBox(
                    f"Appended {summary.added_words} new words (lines {summary.first_line}-{summary.last_line}).\n\n"
                    "Jump straight to Step 2 (Unrecorded Only) to record them now?",
                    f"Reinforcement Pass Ready ({summary.batch_id})",
                    wx.YES_NO | wx.ICON_INFORMATION,
                    parent=self,
                )
                if go_rec == wx.YES:
                    self.frame.go_to_step(2, unrecorded_only=True)
            else:
                wx.MessageBox(
                    f"Appended {summary.added_words} new words (lines {summary.first_line}-{summary.last_line}).\n\n"
                    f"Standalone recording script saved to:\n{summary.script_path}\n\n"
                    "Record ONLY those new words on your external recorder, then go to Step 3 and click "
                    "'Import Pickup / Extension WAV...' (the line range will be pre-filled automatically).",
                    f"Reinforcement Pass Ready ({summary.batch_id})",
                    wx.OK | wx.ICON_INFORMATION,
                    parent=self,
                )

        self.frame.runner.run(
            "Calculate Reinforcement Pass",
            lambda prog, canc: compute_reinforcement_pass(
                proj,
                extra_words=0,
                min_takes=2,
                catalog=self.frame.catalog,
                progress=prog,
                cancel=canc,
            ),
            on_success=_on_preview_ready,
        )

    def _on_speak(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        text = self.txt_preview.GetValue().strip()
        if not text:
            return
        db_name = self._selected_db_name()

        def _task(prog, _canc):
            prog(f"Synthesizing preview with mb-{db_name}...", None)
            return synthesize_preview(proj, db_name, text)

        def _done(wav_path: Path) -> None:
            play_wav_file(wav_path, blocking=False)
            self.frame.announce_status(f"Playing preview ({wav_path.name}).")

        self.frame.runner.run("Synthesize Preview", _task, on_success=_done)

    def _on_save_wav(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        db_name = self._selected_db_name()
        text = self.txt_preview.GetValue().strip()
        if not text:
            return
        with wx.FileDialog(
            self,
            "Save Preview WAV",
            defaultFile=f"{db_name}_preview.wav",
            wildcard="WAV audio (*.wav)|*.wav",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            dest = Path(dlg.GetPath())
        self.frame.runner.run(
            "Save Preview WAV",
            lambda prog, _canc: synthesize_preview(proj, db_name, text, out_wav=dest),
            on_success=lambda p: wx.MessageBox(f"Saved preview WAV to:\n{p}", "WAV Saved", wx.OK | wx.ICON_INFORMATION, parent=self),
        )

    def _on_export(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        db_name = self._selected_db_name()
        with wx.DirDialog(self, "Choose Folder to Export Voice Package Into") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            dest_root = Path(dlg.GetPath())
        try:
            out = export_voice_package(proj, db_name, dest_root)
            wx.MessageBox(f"Exported standalone voice package to:\n{out}", "Voice Exported", wx.OK | wx.ICON_INFORMATION, parent=self)
        except Exception as exc:
            wx.MessageBox(f"Export failed:\n{exc}", "Export Error", wx.OK | wx.ICON_ERROR, parent=self)

    def _on_install_nvda(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        db_name = self._selected_db_name()
        try:
            dest = install_voice_to_nvda(proj, db_name)
            wx.MessageBox(
                f"Installed voice 'mb-{db_name}' into NVDA add-on:\n{dest}\n\n"
                "Restart NVDA (or reload the synthesizer) to select it under eSpeak with MBROLA.",
                "Installed into NVDA",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )
        except Exception as exc:
            wx.MessageBox(f"Install failed:\n{exc}", "NVDA Install Error", wx.OK | wx.ICON_ERROR, parent=self)

    def can_proceed(self) -> bool:
        return False
