"""Screen 1 — Language Configuration, Word List Source & Hybrid Prompt Planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import wx

from ..planner import PlanSummary, generate_plan

if TYPE_CHECKING:
    from .app import WizardFrame


class PlanPanel(wx.Panel):
    """Step 1: Configure word list source and generate the hybrid real-word + supplement prompt list."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.lbl_meta = wx.StaticText(self, label="")
        sizer.Add(self.lbl_meta, 0, wx.BOTTOM, 10)

        # Wordlist & planning options
        cfg_box = wx.StaticBox(self, label="Word Corpus & Script Settings")
        cfg_sizer = wx.StaticBoxSizer(cfg_box, wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        lbl_wl = wx.StaticText(self, label="&Word List Source:")
        wl_row = wx.BoxSizer(wx.HORIZONTAL)
        self.rb_auto = wx.RadioButton(self, label="Auto-download FrequencyWords (~0.5 MB)", style=wx.RB_GROUP)
        self.rb_custom = wx.RadioButton(self, label="Custom word list / plain text file:")
        self.rb_auto.Bind(wx.EVT_RADIOBUTTON, self._on_wl_mode)
        self.rb_custom.Bind(wx.EVT_RADIOBUTTON, self._on_wl_mode)
        wl_row.Add(self.rb_auto, 0, wx.RIGHT, 12)
        wl_row.Add(self.rb_custom, 0)
        grid.Add(lbl_wl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wl_row, 1, wx.EXPAND)

        lbl_path = wx.StaticText(self, label="Custom &File Path:")
        file_row = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_custom = wx.TextCtrl(self)
        self.txt_custom.SetName("Custom Word List File Path")
        self.btn_browse = wx.Button(self, label="Br&owse...")
        self.btn_browse.Bind(wx.EVT_BUTTON, self._on_browse_wl)
        file_row.Add(self.txt_custom, 1, wx.EXPAND | wx.RIGHT, 6)
        file_row.Add(self.btn_browse, 0)
        grid.Add(lbl_path, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(file_row, 1, wx.EXPAND)

        lbl_max = wx.StaticText(self, label="&Maximum Real Words:")
        self.spin_max_words = wx.SpinCtrl(self, min=100, max=1200, initial=500)
        self.spin_max_words.SetName("Maximum Real Words")
        grid.Add(lbl_max, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.spin_max_words, 0)

        lbl_takes = wx.StaticText(self, label="Target &Takes per Pair:")
        self.choice_min_takes = wx.Choice(
            self,
            choices=[
                "1x (compact script, fastest recording ~15-18 min)",
                "2x (redundant takes for common speech — set Max Words ~650-850)",
            ],
        )
        self.choice_min_takes.SetName("Target Takes per Pair")
        self.choice_min_takes.SetSelection(0)
        self.choice_min_takes.Bind(wx.EVT_CHOICE, self._on_min_takes_changed)
        grid.Add(lbl_takes, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.choice_min_takes, 1, wx.EXPAND)

        lbl_supp = wx.StaticText(self, label="&Supplement Tail Mode:")
        self.choice_supp = wx.Choice(
            self,
            choices=[
                "real-gaps (compact: only real-text pairs missed by words)",
                "cv-vc (larger: full theoretical CV/VC matrix)",
            ],
        )
        self.choice_supp.SetName("Supplement Tail Mode")
        self.choice_supp.SetSelection(0)
        grid.Add(lbl_supp, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.choice_supp, 1, wx.EXPAND)

        cfg_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.btn_generate = wx.Button(self, label="&Generate Recording Script Plan")
        self.btn_generate.Bind(wx.EVT_BUTTON, self._on_generate)
        cfg_sizer.Add(self.btn_generate, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(cfg_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)

        # Plan summary & prompt preview
        res_box = wx.StaticBox(self, label="Generated Plan Summary & Prompt Preview")
        res_sizer = wx.StaticBoxSizer(res_box, wx.VERTICAL)

        self.txt_summary = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 78))
        self.txt_summary.SetName("Plan Summary")
        res_sizer.Add(self.txt_summary, 0, wx.EXPAND | wx.ALL, 6)

        self.list_prompts = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_prompts.SetName("Prompt Preview List")
        self.list_prompts.InsertColumn(0, "Line", width=60)
        self.list_prompts.InsertColumn(1, "Type", width=110)
        self.list_prompts.InsertColumn(2, "Prompt", width=220)
        self.list_prompts.InsertColumn(3, "IPA", width=240)
        res_sizer.Add(self.list_prompts, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        sizer.Add(res_sizer, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def on_enter(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        spec = proj.language_spec(self.frame.catalog)
        rates_str = ", ".join(f"{r} Hz" for r in proj.config.rates)
        note_str = f"  |  Note: {spec.notes}" if spec.notes else ""
        self.lbl_meta.SetLabel(
            f"Project: {proj.config.voice_id}  |  Language: {spec.name} ({spec.espeak_voice})  |  "
            f"MFA Model: {spec.mfa_model}  |  Rates: {rates_str}{note_str}"
        )

        self.rb_auto.Enable(spec.has_auto_wordlist)
        if proj.config.wordlist_source == "auto" and spec.has_auto_wordlist:
            self.rb_auto.SetValue(True)
            self.txt_custom.SetValue("")
        else:
            self.rb_custom.SetValue(True)
            self.txt_custom.SetValue("" if proj.config.wordlist_source == "auto" else proj.config.wordlist_source)
        self._on_wl_mode(None)

        self.spin_max_words.SetValue(proj.config.max_words)
        self.choice_min_takes.SetSelection(1 if getattr(proj.config, "min_takes", 1) >= 2 else 0)
        self.choice_supp.SetSelection(0 if proj.config.supplement_mode == "real-gaps" else 1)
        self._refresh_existing_plan()
        if self.rb_auto.IsEnabled() and self.rb_auto.GetValue():
            self.rb_auto.SetFocus()
        else:
            self.rb_custom.SetFocus()

    def _on_min_takes_changed(self, _evt: wx.CommandEvent) -> None:
        if self.choice_min_takes.GetSelection() == 1 and self.spin_max_words.GetValue() < 650:
            self.spin_max_words.SetValue(650)
        elif self.choice_min_takes.GetSelection() == 0 and self.spin_max_words.GetValue() == 650:
            self.spin_max_words.SetValue(500)

    def _on_wl_mode(self, _evt) -> None:
        custom = self.rb_custom.GetValue()
        self.txt_custom.Enable(custom)
        self.btn_browse.Enable(custom)

    def _on_browse_wl(self, _evt: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self,
            "Select Word List or Plain-Text Corpus",
            wildcard="Text files (*.txt)|*.txt|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_custom.SetValue(dlg.GetPath())

    def _refresh_existing_plan(self) -> None:
        proj = self.frame.project
        if proj is None:
            return
        dmap_path = proj.plan_dir / "diphone_map.json"
        inv_path = proj.plan_dir / "inventory.json"
        self.list_prompts.DeleteAllItems()
        if not (dmap_path.is_file() and inv_path.is_file()):
            self.txt_summary.SetValue("No recording script plan generated yet. Click 'Generate Recording Script Plan' to start.")
            self.frame.update_nav_buttons()
            return

        inv = json.loads(inv_path.read_text(encoding="utf-8"))
        dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
        lines = dmap["lines"]
        est_min = max(1, int(round(len(lines) * 2.0 / 60.0)))
        self.txt_summary.SetValue(
            f"Canonical phone inventory: {len(inv['vowels'])} vowels + {len(inv['consonants'])} consonants "
            f"({len(inv['vowels']) + len(inv['consonants'])} phones)\n"
            f"Recording prompts: {dmap['word_count']} real words + {dmap['supplement_count']} nonsense supplements "
            f"= {len(lines)} total (approx. {est_min} min recording time)\n"
            f"Script saved at: {proj.plan_dir / 'recording_script.txt'}"
        )
        for idx, item in enumerate(lines):
            row = self.list_prompts.InsertItem(idx, f"{int(item['line']):04d}")
            kind_lbl = "Real word" if item.get("kind") == "word" else "Nonsense"
            self.list_prompts.SetItem(row, 1, kind_lbl)
            self.list_prompts.SetItem(row, 2, str(item["text"]))
            self.list_prompts.SetItem(row, 3, " ".join(item.get("ipa", [])))
        self.frame.update_nav_buttons()

    def _on_generate(self, _evt: wx.CommandEvent) -> None:
        proj = self.frame.project
        if proj is None:
            return
        if self.rb_auto.GetValue():
            proj.config.wordlist_source = "auto"
        else:
            p = self.txt_custom.GetValue().strip()
            if not p or not Path(p).is_file():
                wx.MessageBox("Choose a valid custom word list or plain-text file.", "Word List Required", wx.OK | wx.ICON_WARNING, parent=self)
                self.txt_custom.SetFocus()
                return
            proj.config.wordlist_source = p

        proj.config.max_words = int(self.spin_max_words.GetValue())
        proj.config.min_takes = 2 if self.choice_min_takes.GetSelection() == 1 else 1
        proj.config.supplement_mode = "real-gaps" if self.choice_supp.GetSelection() == 0 else "cv-vc"
        proj.save()

        def _task(prog, canc) -> PlanSummary:
            return generate_plan(proj, catalog=self.frame.catalog, progress=prog, cancel=canc)

        def _done(summary: PlanSummary) -> None:
            self._refresh_existing_plan()
            self.txt_summary.SetValue(
                f"Plan ready! {summary.vowel_count + summary.consonant_count} phones "
                f"({summary.vowel_count} vowels, {summary.consonant_count} consonants).\n"
                f"Prompts: {summary.word_count} real words + {summary.supplement_count} nonsense supplements "
                f"= {summary.total_prompts} total ({summary.running_speech_pct:.2f}% running speech).\n"
                f"Word list: {summary.wordlist_path}"
            )
            self.frame.announce_status(
                f"Prompt plan generated: {summary.total_prompts} prompts ({summary.running_speech_pct:.1f}% coverage). Click Next to record."
            )

        self.frame.runner.run("Generate Prompt Plan", _task, on_success=_done)

    def can_proceed(self) -> bool:
        proj = self.frame.project
        return bool(proj and (proj.plan_dir / "diphone_map.json").is_file())
