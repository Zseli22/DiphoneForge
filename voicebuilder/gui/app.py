"""Main wxPython Stepped Wizard Window for DiphoneForge Voice Builder."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

import wx

from .. import __version__
from ..builder import _db_name_for_rate
from ..catalog import LanguageCatalog
from ..project import VoiceProject
from .align_panel import AlignPanel
from .build_panel import BuildPanel
from .home_panel import HomePanel
from .mnemonics import install_mnemonic_handler
from .plan_panel import PlanPanel
from .studio_panel import StudioPanel
from .worker import BackgroundTaskRunner


STEP_TITLES = [
    "0. Home",
    "1. Plan",
    "2. Record / Import",
    "3. Align & Audit",
    "4. Build & Preview",
]

STEP_FULL_NAMES = [
    "Step 0 of 4: Home and Project Setup",
    "Step 1 of 4: Language and Recording Script Plan",
    "Step 2 of 4: Record or Import Prompt Audio",
    "Step 3 of 4: Forced Alignment and Quality Audit",
    "Step 4 of 4: Build, Listen and Install Voice",
]


def _add_step_page(book: wx.Simplebook, full_name: str, panel_cls, frame: "WizardFrame"):
    """Wrap each wizard step inside a real Win32 StaticBox grouping so NVDA announces the step on navigation."""
    wrapper = wx.Panel(book)
    wrapper.SetName(full_name)
    base_font = wrapper.GetFont()
    step_box = wx.StaticBox(wrapper, label=full_name)
    step_box.SetFont(base_font.Bold())
    step_sizer = wx.StaticBoxSizer(step_box, wx.VERTICAL)
    panel = panel_cls(step_box, frame)
    panel.SetFont(base_font)
    panel.SetName(full_name)
    step_sizer.Add(panel, 1, wx.EXPAND | wx.ALL, 6)
    wrapper.SetSizer(step_sizer)
    book.AddPage(wrapper, full_name)
    return panel


class WizardFrame(wx.Frame):
    """Accessible stepped wizard frame driving the 5 voice-building stages."""

    def __init__(self, initial_project: Optional[Path] = None) -> None:
        super().__init__(
            None,
            title=f"DiphoneForge Voice Builder v{__version__}",
            size=(960, 700),
        )
        self.SetMinSize((840, 620))
        self.catalog = LanguageCatalog()
        self.project: Optional[VoiceProject] = None
        self.current_step: int = 0
        self._task_determinate: bool = False
        self._last_pct: int = -1

        self.runner = BackgroundTaskRunner(
            on_start=self._on_task_start,
            on_progress=self._on_task_progress,
            on_finish=self._on_task_finish,
        )

        root_panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Top clickable/accessible step navigation bar (no & mnemonics so panel shortcuts never collide)
        step_row = wx.BoxSizer(wx.HORIZONTAL)
        self.step_buttons: List[wx.Button] = []
        for idx, title in enumerate(STEP_TITLES):
            btn = wx.Button(root_panel, label=title)
            btn.SetName(STEP_FULL_NAMES[idx])
            btn.Bind(wx.EVT_BUTTON, lambda _e, s=idx: self.go_to_step(s))
            self.step_buttons.append(btn)
            step_row.Add(btn, 1, wx.EXPAND | (wx.RIGHT if idx < len(STEP_TITLES) - 1 else 0), 6)
        main_sizer.Add(step_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        main_sizer.Add(wx.StaticLine(root_panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Active wizard step container (Simplebook — each step wrapped in a named Win32 StaticBox grouping)
        self.book = wx.Simplebook(root_panel)
        self.home_panel = _add_step_page(self.book, STEP_FULL_NAMES[0], HomePanel, self)
        self.plan_panel = _add_step_page(self.book, STEP_FULL_NAMES[1], PlanPanel, self)
        self.studio_panel = _add_step_page(self.book, STEP_FULL_NAMES[2], StudioPanel, self)
        self.align_panel = _add_step_page(self.book, STEP_FULL_NAMES[3], AlignPanel, self)
        self.build_panel = _add_step_page(self.book, STEP_FULL_NAMES[4], BuildPanel, self)

        self.panels = [
            self.home_panel,
            self.plan_panel,
            self.studio_panel,
            self.align_panel,
            self.build_panel,
        ]

        main_sizer.Add(self.book, 1, wx.EXPAND | wx.ALL, 12)

        # Shared background task progress & log strip (always visible for NVDA progress tracking)
        prog_box = wx.StaticBox(root_panel, label="Activity & Progress")
        prog_sizer = wx.StaticBoxSizer(prog_box, wx.VERTICAL)
        top_prog = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_task = wx.StaticText(root_panel, label="Idle.")
        self.gauge = wx.Gauge(root_panel, range=100, size=(180, 18))
        self.gauge.SetName("Task Progress")
        self.btn_cancel = wx.Button(root_panel, label="&Cancel Task")
        self.btn_cancel.Enable(False)
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda _e: self.runner.request_cancel())
        top_prog.Add(self.lbl_task, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        top_prog.Add(self.gauge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        top_prog.Add(self.btn_cancel, 0, wx.ALIGN_CENTER_VERTICAL)
        prog_sizer.Add(top_prog, 0, wx.EXPAND | wx.ALL, 6)

        self.txt_log = wx.TextCtrl(root_panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 68))
        self.txt_log.SetName("Activity Log")
        prog_sizer.Add(self.txt_log, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        main_sizer.Add(prog_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # Wizard navigation footer
        nav_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_home = wx.Button(root_panel, label="&Home")
        self.btn_back = wx.Button(root_panel, label="< &Back")
        self.btn_next = wx.Button(root_panel, label="&Next >")
        self.btn_home.Bind(wx.EVT_BUTTON, lambda _e: self.go_to_step(0))
        self.btn_back.Bind(wx.EVT_BUTTON, lambda _e: self.go_to_step(max(0, self.current_step - 1)))
        self.btn_next.Bind(wx.EVT_BUTTON, lambda _e: self.go_to_step(min(len(self.panels) - 1, self.current_step + 1)))
        nav_row.Add(self.btn_home, 0, wx.RIGHT, 8)
        nav_row.AddStretchSpacer(1)
        nav_row.Add(self.btn_back, 0, wx.RIGHT, 8)
        nav_row.Add(self.btn_next, 0)
        main_sizer.Add(nav_row, 0, wx.EXPAND | wx.ALL, 12)

        root_panel.SetSizer(main_sizer)
        self.CreateStatusBar()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        install_mnemonic_handler(self)

        if initial_project is not None:
            try:
                proj = VoiceProject.load(initial_project)
                self.set_project(proj)
                self.go_to_step(self.smart_resume_step(proj))
            except Exception as exc:
                wx.MessageBox(f"Could not open initial project:\n{exc}", "Open Error", wx.OK | wx.ICON_ERROR, parent=self)
                self.go_to_step(0)
        else:
            self.go_to_step(0)

    def smart_resume_step(self, proj: VoiceProject) -> int:
        """Pick the most helpful wizard step when reopening an existing project."""
        if not (proj.plan_dir / "diphone_map.json").is_file():
            return 1
        # Check if any voice DB is already built
        active_rates = list(proj.config.rates or [22050])
        for r in (22050, 16000):
            for cand in (
                _db_name_for_rate(proj.config.voice_id, r, active_rates),
                _db_name_for_rate(proj.config.voice_id, r, [22050, 16000]),
                proj.config.voice_id,
            ):
                if (proj.build_dir / cand / cand).is_file():
                    return 4
        if proj.segments_csv.is_file() and proj.audit_file.is_file():
            return 3
        return 2

    def _step_unlocked(self, step: int) -> bool:
        if step == 0:
            return True
        proj = self.project
        if proj is None:
            return False
        if step == 1:
            return True
        has_plan = (proj.plan_dir / "diphone_map.json").is_file()
        if step == 2:
            return has_plan
        if step == 3:
            if not has_plan:
                return False
            if proj.segments_csv.is_file():
                return True
            has_takes = proj.takes_dir.is_dir() and any(p.stat().st_size > 1000 for p in proj.takes_dir.glob("line_*.wav"))
            has_ext = proj.ranges_file.is_file()
            return has_takes or has_ext
        if step == 4:
            return proj.segments_csv.is_file() and proj.audit_file.is_file()
        return False

    def set_project(self, project: VoiceProject) -> None:
        self.project = project
        self.SetTitle(f"DiphoneForge Voice Builder — {project.config.voice_id} ({project.config.language_id})")
        self.update_nav_buttons()

    def announce_status(self, msg: str) -> None:
        self.SetStatusText(msg)
        self.lbl_task.SetLabel(msg)

    def _update_step_banner(self) -> None:
        busy = self.runner.is_busy
        base_font = self.GetFont()
        bold_font = base_font.Bold()
        for idx, (btn, title) in enumerate(zip(self.step_buttons, STEP_TITLES)):
            is_cur = idx == self.current_step
            btn.SetLabel(f"[{title}]" if is_cur else title)
            btn.SetFont(bold_font if is_cur else base_font)
            btn.Enable((not busy) and (is_cur or self._step_unlocked(idx)))

    def update_nav_buttons(self) -> None:
        busy = self.runner.is_busy
        self.btn_home.Enable((not busy) and self.current_step > 0)
        self.btn_back.Enable((not busy) and self.current_step > 0)

        can_next = False
        if not busy and self.project is not None and self.current_step < len(self.panels) - 1:
            if self.current_step == 0:
                can_next = True
            else:
                panel = self.panels[self.current_step]
                can_next = bool(getattr(panel, "can_proceed", lambda: True)())
        self.btn_next.Enable(can_next)
        self._update_step_banner()

    def go_to_step(
        self,
        step: int,
        *,
        flagged_only: Optional[Sequence[int]] = None,
        unrecorded_only: bool = False,
    ) -> None:
        if self.runner.is_busy:
            return
        old_panel = self.panels[self.current_step]
        if hasattr(old_panel, "on_leave"):
            old_panel.on_leave()

        self.current_step = max(0, min(len(self.panels) - 1, step))
        self.book.SetSelection(self.current_step)

        new_panel = self.panels[self.current_step]
        if self.current_step == 2 and (flagged_only is not None or unrecorded_only):
            self.studio_panel.on_enter(flagged_only=flagged_only, unrecorded_only=unrecorded_only)
        elif hasattr(new_panel, "on_enter"):
            new_panel.on_enter()

        self.update_nav_buttons()
        self.announce_status(f"Now on {STEP_TITLES[self.current_step]}")

    def _on_task_start(self, title: str) -> None:
        self._task_determinate = False
        self._last_pct = 0
        self.lbl_task.SetLabel(f"Running: {title}...")
        self.gauge.SetValue(0)
        self.btn_cancel.Enable(True)
        self.txt_log.AppendText(f"\n=== {title} ===\n")
        self.update_nav_buttons()

    def _on_task_progress(self, msg: str, frac: Optional[float]) -> None:
        self.lbl_task.SetLabel(msg)
        self.SetStatusText(msg)
        self.txt_log.AppendText(msg + "\n")
        if frac is not None:
            self._task_determinate = True
            pct = max(0, min(100, int(round(frac * 100))))
            if pct != self._last_pct:
                self._last_pct = pct
                self.gauge.SetValue(pct)
        elif not self._task_determinate:
            self.gauge.Pulse()

    def _on_task_finish(self, ok: bool, msg: str) -> None:
        self.btn_cancel.Enable(False)
        self._last_pct = 100 if ok else 0
        self.gauge.SetValue(self._last_pct)
        self.lbl_task.SetLabel(msg)
        self.SetStatusText(msg)
        self.txt_log.AppendText(msg + "\n")
        self.update_nav_buttons()
        if not ok:
            wx.MessageBox(msg, "Task Stopped", wx.OK | wx.ICON_WARNING, parent=self)

    def _on_close(self, evt: wx.CloseEvent) -> None:
        if self.runner.is_busy:
            res = wx.MessageBox(
                "A background task is currently running. Cancel it and exit?",
                "Confirm Exit",
                wx.YES_NO | wx.ICON_WARNING,
                parent=self,
            )
            if res != wx.YES:
                evt.Veto()
                return
            self.runner.request_cancel()
        if hasattr(self.studio_panel, "on_leave"):
            self.studio_panel.on_leave()
        evt.Skip()


def launch_gui(initial_project: Optional[Path] = None) -> int:
    app = wx.App(False)
    frame = WizardFrame(initial_project=initial_project)
    frame.CenterOnScreen()
    frame.Show()
    app.MainLoop()
    return 0
