"""Screen 0 — Home / Project Launcher & Clean-Slate Setup Doctor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

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


class NewProjectDialog(wx.Dialog):
    """Accessible dialog to create a new voice project folder."""

    def __init__(self, parent: wx.Window, catalog: LanguageCatalog) -> None:
        super().__init__(parent, title="New Voice Project", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.catalog = catalog
        self.specs = catalog.all()

        root_sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
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

        # Recording Mode
        lbl_mode = wx.StaticText(self, label="Recording &Workflow:")
        self.choice_mode = wx.Choice(
            self,
            choices=[
                "Built-in PC Microphone Studio (record line by line)",
                "External Recorder Import (import long WAV files + ranges)",
            ],
        )
        self.choice_mode.SetName("Recording Workflow")
        self.choice_mode.SetSelection(0)
        grid.Add(lbl_mode, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.choice_mode, 1, wx.EXPAND)

        # Sample rates
        lbl_rates = wx.StaticText(self, label="Target &Sample Rates:")
        rate_row = wx.BoxSizer(wx.HORIZONTAL)
        self.chk_22k = wx.CheckBox(self, label="22.05 kHz (default, higher clarity)")
        self.chk_22k.SetValue(True)
        self.chk_16k = wx.CheckBox(self, label="16 kHz (smaller && faster)")
        self.chk_16k.SetValue(False)
        rate_row.Add(self.chk_22k, 0, wx.RIGHT, 12)
        rate_row.Add(self.chk_16k, 0)
        grid.Add(lbl_rates, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(rate_row, 1, wx.EXPAND)

        root_sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        root_sizer.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.txt_vid.Bind(wx.EVT_TEXT, self._on_vid_changed)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.SetSizerAndFit(root_sizer)
        self.SetMinSize((560, 290))
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
        if not self.chk_16k.GetValue() and not self.chk_22k.GetValue():
            wx.MessageBox("Select at least one target sample rate.", "Sample Rate Required", wx.OK | wx.ICON_WARNING, parent=self)
            return
        self.EndModal(wx.ID_OK)

    def create_project(self) -> VoiceProject:
        spec = self.specs[self.choice_lang.GetSelection()]
        rates: List[int] = []
        if self.chk_22k.GetValue():
            rates.append(22050)
        if self.chk_16k.GetValue():
            rates.append(16000)
        mode = "mic" if self.choice_mode.GetSelection() == 0 else "external"
        wl = "auto" if spec.has_auto_wordlist else ""
        return VoiceProject.create(
            Path(self.txt_dir.GetValue()),
            self.txt_vid.GetValue(),
            spec,
            rates=rates,
            wordlist_source=wl,
            recording_mode=mode,
        )


class HomePanel(wx.Panel):
    """Step 0: Create/Open project and check/install clean-slate dependencies."""

    def __init__(self, parent: wx.Window, frame: "WizardFrame") -> None:
        super().__init__(parent)
        self.frame = frame

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "Create a new MBROLA voice project or open an existing one. "
                "Word lists and MFA alignment models are downloaded on demand into the app's portable cache folder."
            ),
        )
        intro.Wrap(720)
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

        lbl_recent = wx.StaticText(self, label="&Recent Projects (press Enter or double-click to open):")
        self.list_recent = wx.ListBox(self, style=wx.LB_SINGLE | wx.WANTS_CHARS)
        self.list_recent.SetName("Recent Projects")
        self.list_recent.SetMinSize((-1, 80))
        self.list_recent.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open_recent)
        self.list_recent.Bind(wx.EVT_LISTBOX, self._on_recent_selection_changed)
        self.list_recent.Bind(wx.EVT_CHAR_HOOK, self._on_recent_key)
        self.list_recent.Bind(wx.EVT_KEY_DOWN, self._on_recent_key)
        proj_sizer.Add(lbl_recent, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        proj_sizer.Add(self.list_recent, 0, wx.EXPAND | wx.ALL, 6)

        self.lbl_current = wx.StaticText(self, label="Current Project: (none loaded)")
        proj_sizer.Add(self.lbl_current, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        sizer.Add(proj_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)

        # Environment & Clean-Slate Setup Doctor
        doc_box = wx.StaticBox(self, label="Environment & On-Demand Downloads Doctor")
        doc_sizer = wx.StaticBoxSizer(doc_box, wx.VERTICAL)

        self.list_doctor = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_doctor.SetName("Environment Doctor Status")
        self.list_doctor.InsertColumn(0, "Component", width=220)
        self.list_doctor.InsertColumn(1, "Status", width=80)
        self.list_doctor.InsertColumn(2, "Details", width=420)
        self.list_doctor.SetMinSize((-1, 150))
        doc_sizer.Add(self.list_doctor, 1, wx.EXPAND | wx.ALL, 6)

        doc_btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(self, label="Re&fresh Status")
        self.btn_install_mfa = wx.Button(self, label="&Install MFA Runtime (On-Demand)")
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self.refresh_doctor())
        self.btn_install_mfa.Bind(wx.EVT_BUTTON, self._on_install_mfa)
        doc_btns.Add(self.btn_refresh, 0, wx.RIGHT, 8)
        doc_btns.Add(self.btn_install_mfa, 0)
        doc_sizer.Add(doc_btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        note = wx.StaticText(
            self,
            label=(
                f"Portable cache folder: {get_cache_root()}  "
                "(Note: word lists and MFA models stay fully portable; a fresh micromamba MFA install "
                "binds to its install path if the app folder is moved later.)"
            ),
        )
        note.Wrap(720)
        doc_sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        sizer.Add(doc_sizer, 1, wx.EXPAND)
        self.SetSizer(sizer)

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
        items = load_recent_projects()
        for p in items:
            self.list_recent.Append(p)
        if items:
            self.list_recent.SetSelection(0)
        self.btn_open_recent.Enable(bool(items))

    def _on_recent_selection_changed(self, _evt: wx.CommandEvent) -> None:
        self.btn_open_recent.Enable(self.list_recent.GetSelection() != wx.NOT_FOUND)

    def refresh_doctor(self) -> None:
        self.list_doctor.DeleteAllItems()
        mfa_ok = False
        for idx, st in enumerate(run_doctor()):
            row = self.list_doctor.InsertItem(idx, st.name)
            self.list_doctor.SetItem(row, 1, "Ready" if st.ok else "Missing")
            self.list_doctor.SetItem(row, 2, st.detail)
            if "Montreal Forced Aligner" in st.name:
                mfa_ok = st.ok
        self.btn_install_mfa.Enable(not mfa_ok)

    def _load_project_dir(self, path: Path) -> None:
        try:
            proj = VoiceProject.load(path)
        except Exception as exc:
            wx.MessageBox(f"Could not open project:\n{exc}", "Open Project Error", wx.OK | wx.ICON_ERROR, parent=self)
            return
        record_recent_project(proj.root)
        self.frame.set_project(proj)
        self.frame.go_to_step(1)

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
        if sel != wx.NOT_FOUND:
            self._load_project_dir(Path(self.list_recent.GetString(sel)))

    def _on_recent_key(self, evt: wx.KeyEvent) -> None:
        if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_open_recent(evt)
        else:
            evt.Skip()

    def _on_install_mfa(self, _evt: wx.CommandEvent) -> None:
        res = wx.MessageBox(
            "This will download micromamba (~10 MB if needed) and install Montreal Forced Aligner "
            "(~2.4 GB one-time download) into the app's cache folder.\n\nProceed?",
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
