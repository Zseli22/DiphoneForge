"""Screen 0 — Home / Project Launcher & Clean-Slate Setup Doctor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, List

import wx

from ..catalog import LanguageCatalog
from ..downloader import ensure_mfa_env, run_doctor
from ..paths import get_app_root, get_cache_root
from ..project import VoiceProject, validate_voice_id
from .mnemonics import install_mnemonic_handler

if TYPE_CHECKING:
    from .app import WizardFrame


def _recent_file() -> Path:
    return get_cache_root() / "recent_projects.json"


def load_recent_projects() -> List[str]:
    f = _recent_file()
    if not f.is_file():
        return []
    try:
        items = json.loads(f.read_text(encoding="utf-8"))
        return [str(p) for p in items if (Path(p) / "project.json").is_file()]
    except Exception:
        return []


def record_recent_project(project_dir: Path) -> None:
    resolved = str(project_dir.resolve())
    items = [p for p in load_recent_projects() if p != resolved]
    items.insert(0, resolved)
    f = _recent_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(items[:10], indent=2) + "\n", encoding="utf-8")


def _describe_project_summary(project_dir: Path) -> str:
    """Build a friendly one-line summary for the Recent Projects list."""
    try:
        proj = VoiceProject.load(project_dir)
    except Exception:
        return str(project_dir)
    status_parts: List[str] = []
    dmap_p = proj.plan_dir / "diphone_map.json"
    if dmap_p.is_file():
        try:
            dmap = json.loads(dmap_p.read_text(encoding="utf-8"))
            total = len(dmap.get("lines", []))
            if proj.config.recording_mode == "mic":
                rec = sum(
                    1
                    for it in dmap.get("lines", [])
                    if (proj.takes_dir / f"line_{int(it['line']):04d}.wav").is_file()
                    and (proj.takes_dir / f"line_{int(it['line']):04d}.wav").stat().st_size > 1000
                )
                status_parts.append(f"{rec}/{total} recorded")
            else:
                status_parts.append(f"{total} prompts (external)")
        except Exception:
            pass
    if proj.config.stages.get("build") == "done":
        status_parts.append("Built")
    elif proj.audit_file.is_file():
        status_parts.append("Aligned")
    elif not status_parts:
        status_parts.append("New")
    summary = ", ".join(status_parts)
    return f"{proj.config.voice_id} ({proj.config.language_id}) — {summary} — {project_dir}"


class NewProjectDialog(wx.Dialog):
    """Streamlined dialog to create a new voice project folder (Language, Voice ID, Folder)."""

    def __init__(self, parent: wx.Window, catalog: LanguageCatalog) -> None:
        super().__init__(parent, title="New Voice Project", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.catalog = catalog
        self.specs = catalog.all()

        root_sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=10)
        grid.AddGrowableCol(1, 1)

        # Language
        lbl_lang = wx.StaticText(self, label="&Language:")
        self.choice_lang = wx.Choice(self, choices=[s.display_label for s in self.specs])
        self.choice_lang.SetName("Language")
        self.choice_lang.SetSelection(0)
        grid.Add(lbl_lang, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.choice_lang, 1, wx.EXPAND)

        # Voice ID
        lbl_vid = wx.StaticText(self, label="&Voice ID (2-16 lowercase letters/digits):")
        self.txt_vid = wx.TextCtrl(self, value="myvoice")
        self.txt_vid.SetName("Voice ID")
        grid.Add(lbl_vid, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.txt_vid, 1, wx.EXPAND)

        # Project folder
        lbl_dir = wx.StaticText(self, label="Project &Folder:")
        dir_row = wx.BoxSizer(wx.HORIZONTAL)
        default_dir = get_app_root() / "projects" / "myvoice"
        self.txt_dir = wx.TextCtrl(self, value=str(default_dir))
        self.txt_dir.SetName("Project Folder")
        btn_browse = wx.Button(self, label="&Browse...")
        btn_browse.Bind(wx.EVT_BUTTON, self._on_browse)
        dir_row.Add(self.txt_dir, 1, wx.EXPAND | wx.RIGHT, 6)
        dir_row.Add(btn_browse, 0)
        grid.Add(lbl_dir, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(dir_row, 1, wx.EXPAND)

        root_sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)

        hint = wx.StaticText(
            self,
            label="Recording workflow (PC mic vs external recorder) is chosen on Step 2; target sample rates are chosen on Step 4.",
        )
        hint.Wrap(520)
        root_sizer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        root_sizer.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.txt_vid.Bind(wx.EVT_TEXT, self._on_vid_changed)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.SetSizerAndFit(root_sizer)
        self.SetMinSize((560, 220))
        install_mnemonic_handler(self)
        self.choice_lang.SetFocus()

    def _on_vid_changed(self, _evt: wx.CommandEvent) -> None:
        vid = self.txt_vid.GetValue().strip().lower()
        if vid:
            cur = Path(self.txt_dir.GetValue())
            if cur.parent == (get_app_root() / "projects"):
                self.txt_dir.SetValue(str(cur.parent / vid))

    def _on_browse(self, _evt: wx.CommandEvent) -> None:
        with wx.DirDialog(self, "Choose Voice Project Folder", defaultPath=self.txt_dir.GetValue()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_dir.SetValue(dlg.GetPath())

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        try:
            validate_voice_id(self.txt_vid.GetValue())
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid Voice ID", wx.OK | wx.ICON_WARNING, parent=self)
            self.txt_vid.SetFocus()
            return
        self.EndModal(wx.ID_OK)

    def create_project(self) -> VoiceProject:
        spec = self.specs[self.choice_lang.GetSelection()]
        wl = "auto" if spec.has_auto_wordlist else ""
        return VoiceProject.create(
            Path(self.txt_dir.GetValue()),
            self.txt_vid.GetValue(),
            spec,
            rates=[22050],
            wordlist_source=wl,
            recording_mode="mic",
            max_words=0,
            include_supplements=True,
        )


class HomePanel(wx.Panel):
    """Step 0: Create/Open project and check/install clean-slate dependencies."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame
        self._recent_paths: List[str] = []
        self._doctor_expanded: bool = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "Create a new MBROLA voice project or open an existing one. "
                "Word lists, Hunspell dictionaries, and MFA alignment models are downloaded on demand into the portable cache folder."
            ),
        )
        intro.Wrap(760)
        sizer.Add(intro, 0, wx.BOTTOM, 10)

        # Project actions
        proj_box = wx.StaticBox(self, label="Voice Project")
        proj_sizer = wx.StaticBoxSizer(proj_box, wx.VERTICAL)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_new = wx.Button(self, label="New &Project...")
        self.btn_open = wx.Button(self, label="&Open Existing Project...")
        self.btn_open_recent = wx.Button(self, label="Open &Selected Recent")
        self.btn_new.Bind(wx.EVT_BUTTON, self._on_new_project)
        self.btn_open.Bind(wx.EVT_BUTTON, self._on_open_project)
        self.btn_open_recent.Bind(wx.EVT_BUTTON, self._on_open_recent)
        btn_row.Add(self.btn_new, 0, wx.RIGHT, 8)
        btn_row.Add(self.btn_open, 0, wx.RIGHT, 8)
        btn_row.Add(self.btn_open_recent, 0)
        proj_sizer.Add(btn_row, 0, wx.ALL, 6)

        lbl_recent = wx.StaticText(self, label="&Recent Projects (press Enter or double-click to resume):")
        self.list_recent = wx.ListBox(self, style=wx.LB_SINGLE)
        self.list_recent.SetName("Recent Projects")
        self.list_recent.SetMinSize((-1, 110))
        self.list_recent.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open_recent)
        self.list_recent.Bind(wx.EVT_LISTBOX, self._on_recent_selection_changed)
        self.list_recent.Bind(wx.EVT_CHAR_HOOK, self._on_recent_key)
        proj_sizer.Add(lbl_recent, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        proj_sizer.Add(self.list_recent, 1, wx.EXPAND | wx.ALL, 6)

        self.lbl_current = wx.StaticText(self, label="Current Project: (none loaded)")
        proj_sizer.Add(self.lbl_current, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        sizer.Add(proj_sizer, 1, wx.EXPAND | wx.BOTTOM, 10)

        # Compact Environment & Clean-Slate Setup Doctor
        doc_box = wx.StaticBox(self, label="Environment & On-Demand Tools")
        self.doc_sizer = wx.StaticBoxSizer(doc_box, wx.VERTICAL)

        top_doc = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_doc_summary = wx.StaticText(self, label="Checking environment...")
        self.btn_toggle_doc = wx.Button(self, label="Environment &Details")
        self.btn_refresh = wx.Button(self, label="Re&fresh")
        self.btn_install_mfa = wx.Button(self, label="&Install MFA Runtime")
        self.btn_toggle_doc.Bind(wx.EVT_BUTTON, self._on_toggle_doctor)
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self.refresh_doctor())
        self.btn_install_mfa.Bind(wx.EVT_BUTTON, self._on_install_mfa)
        top_doc.Add(self.lbl_doc_summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        top_doc.Add(self.btn_toggle_doc, 0, wx.RIGHT, 6)
        top_doc.Add(self.btn_refresh, 0, wx.RIGHT, 6)
        top_doc.Add(self.btn_install_mfa, 0)
        self.doc_sizer.Add(top_doc, 0, wx.EXPAND | wx.ALL, 6)

        self.doc_details_panel = wx.Panel(self)
        det_s = wx.BoxSizer(wx.VERTICAL)
        self.list_doctor = wx.ListCtrl(self.doc_details_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_doctor.SetName("Environment Doctor Status")
        self.list_doctor.InsertColumn(0, "Component", width=230)
        self.list_doctor.InsertColumn(1, "Status", width=80)
        self.list_doctor.InsertColumn(2, "Details", width=440)
        self.list_doctor.SetMinSize((-1, 130))
        det_s.Add(self.list_doctor, 1, wx.EXPAND | wx.BOTTOM, 4)

        note = wx.StaticText(
            self.doc_details_panel,
            label=f"Portable cache folder: {get_cache_root()}",
        )
        det_s.Add(note, 0)
        self.doc_details_panel.SetSizer(det_s)
        self.doc_sizer.Add(self.doc_details_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        sizer.Add(self.doc_sizer, 0, wx.EXPAND)
        self.SetSizer(sizer)

    def _set_doctor_expanded(self, expanded: bool) -> None:
        self._doctor_expanded = expanded
        self.doc_details_panel.Show(expanded)
        self.btn_toggle_doc.SetLabel("Hide &Details" if expanded else "Environment &Details")
        self.Layout()

    def _on_toggle_doctor(self, _evt: wx.CommandEvent) -> None:
        self._set_doctor_expanded(not self._doctor_expanded)

    def on_enter(self) -> None:
        self.refresh_recent()
        self.refresh_doctor()
        if self.frame.project is not None:
            p = self.frame.project
            self.lbl_current.SetLabel(
                f"Current Project: {p.config.voice_id} ({p.config.language_id}) — {p.root}"
            )
        else:
            self.lbl_current.SetLabel("Current Project: (none loaded)")
        self.btn_new.SetFocus()

    def refresh_recent(self) -> None:
        self.list_recent.Clear()
        self._recent_paths = load_recent_projects()
        for p in self._recent_paths:
            self.list_recent.Append(_describe_project_summary(Path(p)))
        if self._recent_paths:
            self.list_recent.SetSelection(0)
        self.btn_open_recent.Enable(bool(self._recent_paths))

    def _on_recent_selection_changed(self, _evt: wx.CommandEvent) -> None:
        self.btn_open_recent.Enable(self.list_recent.GetSelection() != wx.NOT_FOUND)

    def refresh_doctor(self) -> None:
        self.list_doctor.DeleteAllItems()
        statuses = run_doctor()
        mfa_ok = False
        missing_names: List[str] = []
        cache_detail = ""
        for idx, st in enumerate(statuses):
            row = self.list_doctor.InsertItem(idx, st.name)
            self.list_doctor.SetItem(row, 1, "Ready" if st.ok else "Missing")
            self.list_doctor.SetItem(row, 2, st.detail)
            if "Montreal Forced Aligner" in st.name:
                mfa_ok = st.ok
            if st.name == "Portable Cache":
                cache_detail = st.detail
            if not st.ok:
                missing_names.append(st.name)
        self.btn_install_mfa.Show(not mfa_ok)
        self.btn_install_mfa.Enable(not mfa_ok)
        if not missing_names:
            self.lbl_doc_summary.SetLabel(f"All tools ready (eSpeak NG, MBROLATOR, MFA)  |  Cache: {cache_detail}")
            self._set_doctor_expanded(False)
        else:
            self.lbl_doc_summary.SetLabel(f"Setup needed: {', '.join(missing_names)}")
            self._set_doctor_expanded(True)

    def _load_project_dir(self, path: Path) -> None:
        try:
            proj = VoiceProject.load(path)
        except Exception as exc:
            wx.MessageBox(f"Could not open project:\n{exc}", "Open Project Error", wx.OK | wx.ICON_ERROR, parent=self)
            return
        record_recent_project(proj.root)
        self.frame.set_project(proj)
        self.frame.go_to_step(self.frame.smart_resume_step(proj))

    def _on_new_project(self, _evt: wx.CommandEvent) -> None:
        with NewProjectDialog(self, self.frame.catalog) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                try:
                    proj = dlg.create_project()
                except Exception as exc:
                    wx.MessageBox(f"Could not create project:\n{exc}", "Create Project Error", wx.OK | wx.ICON_ERROR, parent=self)
                    return
                record_recent_project(proj.root)
                self.frame.set_project(proj)
                self.frame.go_to_step(1)

    def _on_open_project(self, _evt: wx.CommandEvent) -> None:
        with wx.DirDialog(self, "Select Voice Project Folder (containing project.json)") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._load_project_dir(Path(dlg.GetPath()))

    def _on_open_recent(self, _evt: wx.CommandEvent) -> None:
        sel = self.list_recent.GetSelection()
        if 0 <= sel < len(self._recent_paths):
            self._load_project_dir(Path(self._recent_paths[sel]))

    def _on_recent_key(self, evt: wx.KeyEvent) -> None:
        if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_open_recent(evt)
        else:
            evt.Skip()

    def _on_install_mfa(self, _evt: wx.CommandEvent) -> None:
        res = wx.MessageBox(
            "This will download micromamba (~10 MB if needed) and install Montreal Forced Aligner "
            "(~415 MB one-time download, ~2.4 GB installed) into the app's cache folder.\n\nProceed?",
            "Install MFA Runtime",
            wx.YES_NO | wx.ICON_QUESTION,
            parent=self,
        )
        if res != wx.YES:
            return
        self.frame.runner.run(
            "Install MFA Runtime",
            lambda prog, canc: ensure_mfa_env(progress=prog, cancel=canc),
            on_success=lambda _res: self.refresh_doctor(),
        )
