"""Accessible Interactive Take Picker for auditioning and pinning winning diphone takes.

Features:
  - Lists every recorded diphone sorted by most takes first (most choices at the top).
  - Live filter box for diphone substring (e.g. `_L`, `I_L`) OR click 'Filter by Word/Phrase'
    (e.g. 'fill the pool') to decompose any phrase with eSpeak NG into its exact diphones.
  - Audition each take either as the full recorded word ('Play Word', W) or as the exact
    diphone cut ('Play Diphone', D / Space).
  - Pin any take as winner ('Pin Winner', Enter) -> saved atomically to `take_overrides.json`.
"""

from __future__ import annotations

import collections
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf
import wx

from ..paths import get_app_root, get_espeak_paths
from .mnemonics import dispatch_alt_mnemonic

_TOOLS_DIR = get_app_root() / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def phrase_to_diphones(phrase: str, voice: str, inventory: Optional[Dict[str, object]] = None) -> List[str]:
    """Phonemize a word or phrase through eSpeak NG into canonical diphones (including clear l vs dark L)."""
    import plan_word_corpus as P

    espeak_bin, espeak_data = get_espeak_paths()
    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(espeak_data.resolve())
    hidden_kw = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        hidden_kw["startupinfo"] = si
        hidden_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    res = subprocess.run(
        [str(espeak_bin), "-v", voice, "-q", "-x", "--sep=_", phrase],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=True,
        **hidden_kw,
    )
    raw_words = [w for w in res.stdout.strip().split() if w]
    v_set_pre, c_set_pre, norm_map = P.get_normalization(voice, "auto")
    if inventory is not None:
        vowels = set(inventory.get("vowels", []))
        consonants = set(inventory.get("consonants", []))
        canonical: Optional[Set[str]] = vowels | consonants
        if inventory.get("normalization"):
            norm_map = {k: tuple(v) for k, v in inventory["normalization"].items()}
    else:
        canonical = (v_set_pre | c_set_pre) if (v_set_pre and c_set_pre) else None
        vowels = v_set_pre or set()

    all_phones: List[str] = []
    for rw in raw_words:
        parsed = P.normalize_word_phones(rw, "", norm_map, canonical)
        if parsed is not None:
            all_phones.extend(parsed[0])
        else:
            # Handle 1-phone words (e.g. "a", "I") gracefully
            toks = [t.lstrip("',") for t in rw.split("_") if t.lstrip("',")]
            for tok in toks:
                if tok in norm_map:
                    all_phones.extend(norm_map[tok])
                elif canonical is None or tok in canonical:
                    all_phones.append(tok)

    diphones: List[str] = []
    for a, b in zip(all_phones, all_phones[1:]):
        dp = f"{a}_{b}"
        if dp not in diphones:
            diphones.append(dp)
    return diphones


class TakePickerDialog(wx.Dialog):
    """Accessible dialog to browse diphones (sorted by take count), listen to takes, and pin winners."""

    def __init__(
        self,
        parent: Optional[wx.Window],
        *,
        unit_grid_csv: Path,
        segments_csv: Path,
        audio_dir: Path,
        overrides_json: Path,
        voice: str = "en-gb",
        inventory_json: Optional[Path] = None,
        diphone_map_json: Optional[Path] = None,
        initial_phrase: Optional[str] = None,
    ) -> None:
        super().__init__(
            parent,
            title="Interactive Diphone Take Picker",
            size=(980, 640),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.unit_grid_csv = unit_grid_csv
        self.segments_csv = segments_csv
        self.audio_dir = audio_dir
        self.overrides_json = overrides_json
        self.voice = voice
        self.inventory = json.loads(inventory_json.read_text(encoding="utf-8")) if (inventory_json and inventory_json.is_file()) else None
        self._initial_phrase = initial_phrase.strip() if initial_phrase else None

        # Optional corpus running-speech diphone frequencies for "Most used in speech first" sort
        self.diphone_freq: Dict[str, float] = {}
        if diphone_map_json and diphone_map_json.is_file():
            try:
                dmap_doc = json.loads(diphone_map_json.read_text(encoding="utf-8"))
                raw_freq = dmap_doc.get("diphone_freq") or {}
                self.diphone_freq = {str(k): float(v) for k, v in raw_freq.items()}
            except Exception:
                pass

        self.segments: Dict[int, Dict[str, str]] = {}
        with segments_csv.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.segments[int(r["line"])] = r

        self.by_diphone: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
        with unit_grid_csv.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.by_diphone[r["diphone"]].append(r)
        for dp in self.by_diphone:
            self.by_diphone[dp].sort(key=lambda r: (-float(r["score"]), int(r["line"])))

        # Fallback usage-frequency estimate when diphone_map.json predates diphone_freq
        if not self.diphone_freq:
            for dp, takes in self.by_diphone.items():
                w = 0.0
                for t in takes:
                    rk = int(t.get("vocab_rank", 99999) or 99999)
                    w += 1.0 / math.log2(max(2, rk) + 2.0)
                self.diphone_freq[dp] = w

        self.overrides: Dict[str, int] = {}
        if overrides_json.is_file():
            raw = json.loads(overrides_json.read_text(encoding="utf-8"))
            self.overrides = {str(k): int(v) for k, v in raw.items()}

        self.all_diphones: List[str] = list(self.by_diphone.keys())
        self.visible_diphones: List[str] = []
        self.current_diphone: Optional[str] = None
        self.changed = False

        self._build_ui()
        if self._initial_phrase:
            self.chk_multi_only.SetValue(False)
            self.txt_filter.SetValue(self._initial_phrase)
            self._on_filter_phrase(None)
        else:
            # Default: hide 1-take diphones and sort by most used in speech first
            self.chk_multi_only.SetValue(True)
            self._on_substring_filter(None)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        # Filter & Sort bar (Row 1: search / phrase / clear)
        flt_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_f = wx.StaticText(self, label="&Filter (type substring, or Enter for phrase):")
        self.txt_filter = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.txt_filter.SetName("Filter by diphone or word")
        self.txt_filter.Bind(wx.EVT_TEXT, self._on_substring_filter)
        self.txt_filter.Bind(wx.EVT_TEXT_ENTER, self._on_filter_phrase)
        btn_phrase = wx.Button(self, label="Filter by Word/P&hrase")
        btn_clear = wx.Button(self, label="&Clear")
        btn_phrase.Bind(wx.EVT_BUTTON, self._on_filter_phrase)
        btn_clear.Bind(wx.EVT_BUTTON, self._on_clear_filter)

        flt_row.Add(lbl_f, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        flt_row.Add(self.txt_filter, 1, wx.RIGHT, 6)
        flt_row.Add(btn_phrase, 0, wx.RIGHT, 8)
        flt_row.Add(btn_clear, 0)
        root.Add(flt_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Row 2: Sort order, Hide 1-take diphones, Needs review, Auto-play diphone on arrow
        opt_row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_sort = wx.StaticText(self, label="S&ort:")
        self.choice_sort = wx.Choice(
            self,
            choices=[
                "Most used in speech first",
                "Most takes first",
                "Worst score first",
                "Alphabetical",
            ],
        )
        self.choice_sort.SetName("Sort Diphones")
        self.choice_sort.SetSelection(0)
        self.choice_sort.Bind(wx.EVT_CHOICE, self._on_substring_filter)

        self.chk_multi_only = wx.CheckBox(self, label="Hide 1-take diphones (2+ &takes)")
        self.chk_multi_only.SetName("Hide single-take diphones")
        self.chk_multi_only.Bind(wx.EVT_CHECKBOX, self._on_substring_filter)

        self.chk_needs_review = wx.CheckBox(self, label="Needs re&view")
        self.chk_needs_review.SetToolTip("Show only low-confidence, low-SNR, or audit-flagged winning takes.")
        self.chk_needs_review.Bind(wx.EVT_CHECKBOX, self._on_substring_filter)

        self.chk_autoplay = wx.CheckBox(self, label="&Auto-play diphone on arrow")
        self.chk_autoplay.SetName("Auto-play diphone when arrowing through takes")
        self.chk_autoplay.SetToolTip("Automatically play each diphone cut as you arrow up/down in the Recorded Takes list.")
        self.chk_autoplay.SetValue(False)

        opt_row.Add(lbl_sort, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        opt_row.Add(self.choice_sort, 0, wx.RIGHT, 14)
        opt_row.Add(self.chk_multi_only, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        opt_row.Add(self.chk_needs_review, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        opt_row.Add(self.chk_autoplay, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(opt_row, 0, wx.EXPAND | wx.ALL, 10)

        self.lbl_filter_info = wx.StaticText(self, label="")
        root.Add(self.lbl_filter_info, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Split pane: Left = Diphones, Right = Takes for selected diphone
        split_row = wx.BoxSizer(wx.HORIZONTAL)

        left_box = wx.StaticBox(self, label="Diphones")
        ls = wx.StaticBoxSizer(left_box, wx.VERTICAL)
        self.list_diphones = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_diphones.SetName("Diphones List")
        self.list_diphones.InsertColumn(0, "Diphone", width=85)
        self.list_diphones.InsertColumn(1, "Takes", width=55)
        self.list_diphones.InsertColumn(2, "Winner", width=130)
        self.list_diphones.InsertColumn(3, "Mode", width=65)
        self.list_diphones.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_diphone_selected)
        ls.Add(self.list_diphones, 1, wx.EXPAND | wx.ALL, 6)
        split_row.Add(ls, 2, wx.EXPAND | wx.RIGHT, 8)

        right_box = wx.StaticBox(
            self,
            label="Recorded Takes for Selected Diphone (In this list: W = Play Word, D = Play Diphone, Enter = Pin, R = Reset)",
        )
        rs = wx.StaticBoxSizer(right_box, wx.VERTICAL)
        self.list_takes = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list_takes.SetName("Recorded Takes List")
        self.list_takes.InsertColumn(0, "Line", width=55)
        self.list_takes.InsertColumn(1, "Word", width=130)
        self.list_takes.InsertColumn(2, "Score", width=60)
        self.list_takes.InsertColumn(3, "Pos", width=65)
        self.list_takes.InsertColumn(4, "P1/P2 ms", width=85)
        self.list_takes.InsertColumn(5, "SNR", width=55)
        self.list_takes.InsertColumn(6, "Status", width=75)
        self.list_takes.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self._play_selected_take(diphone_only=False))
        self.list_takes.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_take_selected)
        rs.Add(self.list_takes, 1, wx.EXPAND | wx.ALL, 6)

        tbtns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_play_word = wx.Button(self, label="Play &Word (W)")
        self.btn_play_dp = wx.Button(self, label="Play &Diphone (D)")
        self.btn_pin = wx.Button(self, label="&Pin as Winner")
        self.btn_reset = wx.Button(self, label="&Reset to Auto")
        self.btn_play_word.Bind(wx.EVT_BUTTON, lambda _e: self._play_selected_take(diphone_only=False))
        self.btn_play_dp.Bind(wx.EVT_BUTTON, lambda _e: self._play_selected_take(diphone_only=True))
        self.btn_pin.Bind(wx.EVT_BUTTON, lambda _e: self._pin_selected_take())
        self.btn_reset.Bind(wx.EVT_BUTTON, lambda _e: self._reset_current_diphone())
        for b in (self.btn_play_word, self.btn_play_dp, self.btn_pin, self.btn_reset):
            tbtns.Add(b, 0, wx.RIGHT, 6)
        rs.Add(tbtns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        split_row.Add(rs, 3, wx.EXPAND)

        root.Add(split_row, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Footer
        foot = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_status = wx.StaticText(self, label="")
        btn_close = wx.Button(self, wx.ID_CLOSE, label="&Save && Close")
        btn_close.Bind(wx.EVT_BUTTON, self._on_save_close)
        foot.Add(self.lbl_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        foot.Add(btn_close, 0)
        root.Add(foot, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(root)
        self._update_status_label()
        self.list_diphones.SetFocus()

    def _winner_for(self, dp: str) -> Tuple[Dict[str, str], bool]:
        takes = self.by_diphone[dp]
        pinned_ln = self.overrides.get(dp)
        if pinned_ln is not None:
            for t in takes:
                if int(t["line"]) == pinned_ln:
                    return t, True
        return takes[0], False

    @staticmethod
    def _focus_list_item(ctrl: wx.ListCtrl, idx: int) -> None:
        if 0 <= idx < ctrl.GetItemCount():
            ctrl.Select(idx)
            ctrl.Focus(idx)
            ctrl.SetItemState(
                idx,
                wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
            )
            ctrl.EnsureVisible(idx)

    def _populate_diphones(self, *, select_dp: Optional[str] = None) -> None:
        self.list_diphones.DeleteAllItems()
        sel_idx = 0
        for i, dp in enumerate(self.visible_diphones):
            win, pinned = self._winner_for(dp)
            row = self.list_diphones.InsertItem(i, dp)
            self.list_diphones.SetItem(row, 1, str(len(self.by_diphone[dp])))
            self.list_diphones.SetItem(row, 2, str(win["text"]))
            self.list_diphones.SetItem(row, 3, "Pinned" if pinned else "Auto")
            if select_dp and dp == select_dp:
                sel_idx = i
        self.lbl_filter_info.SetLabel(
            f"Showing {len(self.visible_diphones)} / {len(self.all_diphones)} recorded diphones "
            f"({sum(1 for dp in self.all_diphones if len(self.by_diphone[dp]) >= 2)} have 2+ takes)."
        )
        if self.visible_diphones:
            self._focus_list_item(self.list_diphones, sel_idx)
            self._load_takes_for(self.visible_diphones[sel_idx])
        else:
            self.current_diphone = None
            self.list_takes.DeleteAllItems()

    def _load_takes_for(self, dp: str) -> None:
        self.current_diphone = dp
        self.list_takes.DeleteAllItems()
        takes = self.by_diphone[dp]
        win, pinned = self._winner_for(dp)
        win_ln = int(win["line"])
        sel_row = 0
        for i, t in enumerate(takes):
            ln = int(t["line"])
            row = self.list_takes.InsertItem(i, f"{ln:04d}")
            self.list_takes.SetItem(row, 1, str(t["text"]))
            self.list_takes.SetItem(row, 2, f"{float(t['score']):.2f}")
            pos = "medial" if int(t["is_medial"]) else ("final" if int(t["is_final"]) else "initial")
            self.list_takes.SetItem(row, 3, pos)
            d1 = int(round(float(t["phone1_duration_s"]) * 1000))
            d2 = int(round(float(t["phone2_duration_s"]) * 1000))
            self.list_takes.SetItem(row, 4, f"{d1}/{d2}")
            self.list_takes.SetItem(row, 5, f"{float(t['snr_db']):.1f}")
            st = ("PINNED" if pinned else "AUTO") if ln == win_ln else ""
            self.list_takes.SetItem(row, 6, st)
            if ln == win_ln:
                sel_row = i
        if takes:
            self._focus_list_item(self.list_takes, sel_row)

    def _refresh_current_diphone_in_place(self, *, keep_take_idx: Optional[int] = None) -> None:
        """Update the active diphone row and take statuses in place so neither list loses focus or scroll position."""
        dp = self.current_diphone
        if not dp:
            return
        win, pinned = self._winner_for(dp)
        win_ln = int(win["line"])
        if dp in self.visible_diphones:
            dp_row = self.visible_diphones.index(dp)
            if 0 <= dp_row < self.list_diphones.GetItemCount():
                self.list_diphones.SetItem(dp_row, 2, str(win["text"]))
                self.list_diphones.SetItem(dp_row, 3, "Pinned" if pinned else "Auto")

        takes = self.by_diphone[dp]
        for i, t in enumerate(takes):
            if i < self.list_takes.GetItemCount():
                ln = int(t["line"])
                st = ("PINNED" if pinned else "AUTO") if ln == win_ln else ""
                self.list_takes.SetItem(i, 6, st)
        if keep_take_idx is not None and 0 <= keep_take_idx < self.list_takes.GetItemCount():
            self._focus_list_item(self.list_takes, keep_take_idx)

    def _on_diphone_selected(self, evt: wx.ListEvent) -> None:
        idx = evt.GetIndex()
        if 0 <= idx < len(self.visible_diphones):
            self._load_takes_for(self.visible_diphones[idx])

    def _on_take_selected(self, _evt: wx.ListEvent) -> None:
        if self.chk_autoplay.GetValue() and wx.Window.FindFocus() is self.list_takes:
            wx.CallAfter(self._play_selected_take, diphone_only=True)

    def _sort_diphones(self, dps: List[str]) -> List[str]:
        mode = self.choice_sort.GetSelection()
        if mode == 0:  # Most used in speech first
            return sorted(dps, key=lambda dp: (-self.diphone_freq.get(dp, 0.0), -len(self.by_diphone[dp]), dp))
        if mode == 1:  # Most takes first
            return sorted(dps, key=lambda dp: (-len(self.by_diphone[dp]), dp))
        if mode == 2:  # Worst score first
            return sorted(dps, key=lambda dp: (float(self._winner_for(dp)[0]["score"]), dp))
        return sorted(dps)

    def _is_low_confidence(self, dp: str) -> bool:
        win, _ = self._winner_for(dp)
        score = float(win.get("score", 0.0))
        snr = float(win.get("snr_db", 20.0))
        flagged = int(win.get("is_flagged", 0) or 0)
        mid_drop = float(win.get("mid_drop_db", 0.0) or 0.0)
        return flagged == 1 or score < 0.0 or snr < 14.0 or mid_drop > 16.0

    def _on_substring_filter(self, _evt) -> None:
        q = self.txt_filter.GetValue().strip().lower()
        multi = self.chk_multi_only.GetValue()
        needs_rev = self.chk_needs_review.GetValue()
        filtered = [
            dp for dp in self.all_diphones
            if (not multi or len(self.by_diphone[dp]) >= 2)
            and (not needs_rev or self._is_low_confidence(dp))
            and (not q or q in dp.lower() or any(q in t["text"].lower() for t in self.by_diphone[dp]))
        ]
        self.visible_diphones = self._sort_diphones(filtered)
        self._populate_diphones()

    def _on_filter_phrase(self, _evt) -> None:
        phrase = self.txt_filter.GetValue().strip()
        if not phrase:
            self._on_clear_filter(None)
            return
        try:
            dps = phrase_to_diphones(phrase, self.voice, self.inventory)
        except Exception as exc:
            wx.MessageBox(f"Could not phonemize {phrase!r}:\n{exc}", "Phonemize Error", wx.OK | wx.ICON_WARNING, parent=self)
            return
        present = [dp for dp in dps if dp in self.by_diphone]
        missing = [dp for dp in dps if dp not in self.by_diphone]
        self.visible_diphones = present
        self._populate_diphones()
        miss_note = f"  (Cross-spliced / unrecorded in phrase: {', '.join(missing)})" if missing else ""
        self.lbl_filter_info.SetLabel(
            f"Phrase '{phrase}' -> {len(present)}/{len(dps)} recorded diphones: {', '.join(present)}{miss_note}"
        )

    def _on_clear_filter(self, _evt) -> None:
        self.txt_filter.SetValue("")
        self.chk_needs_review.SetValue(False)
        self._on_substring_filter(None)

    def _play_selected_take(self, *, diphone_only: bool) -> None:
        if not self.current_diphone:
            return
        sel = self.list_takes.GetFirstSelected()
        if sel < 0:
            return
        take = self.by_diphone[self.current_diphone][sel]
        ln = int(take["line"])
        seg = self.segments.get(ln)
        if not seg:
            return
        wav_path = self.audio_dir / seg["source_wav"]
        if not wav_path.is_file():
            wx.MessageBox(f"Source WAV not found: {wav_path}", "Audio Missing", wx.OK | wx.ICON_WARNING, parent=self)
            return

        src_start = float(seg["source_start_s"])
        if diphone_only:
            m1 = 0.5 * (float(take["phone1_start_s"]) + float(take["phone1_end_s"]))
            m2 = 0.5 * (float(take["phone2_start_s"]) + float(take["phone2_end_s"]))
            t0 = max(0.0, src_start + m1 - 0.025)
            t1 = src_start + m2 + 0.025
        else:
            t0 = max(0.0, src_start + float(take["word_start_s"]) - 0.08)
            t1 = src_start + float(take["word_end_s"]) + 0.08

        info = sf.info(str(wav_path))
        sr = info.samplerate
        s0 = max(0, int(round(t0 * sr)))
        frames = max(1, min(info.frames - s0, int(round((t1 - t0) * sr))))
        audio, _ = sf.read(str(wav_path), start=s0, frames=frames, dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        # Gentle 10 ms edge fade to avoid speaker click
        fade = min(len(mono) // 4, int(sr * 0.010))
        if fade > 1:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            mono[:fade] *= ramp
            mono[-fade:] *= ramp[::-1]
        sd.stop()
        sd.play(mono, sr, blocking=False)

    def _save_overrides(self) -> None:
        self.overrides_json.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(sorted(self.overrides.items())), indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.overrides_json)

    def _pin_selected_take(self) -> None:
        if not self.current_diphone:
            return
        sel = self.list_takes.GetFirstSelected()
        if sel < 0:
            return
        take = self.by_diphone[self.current_diphone][sel]
        self.overrides[self.current_diphone] = int(take["line"])
        self.changed = True
        self._save_overrides()
        self._refresh_current_diphone_in_place(keep_take_idx=sel)
        self._update_status_label()

    def _reset_current_diphone(self) -> None:
        if not self.current_diphone:
            return
        if self.current_diphone in self.overrides:
            del self.overrides[self.current_diphone]
            self.changed = True
            self._save_overrides()
            sel = self.list_takes.GetFirstSelected()
            self._refresh_current_diphone_in_place(keep_take_idx=sel if sel >= 0 else None)
            self._update_status_label()

    def _update_status_label(self) -> None:
        self.lbl_status.SetLabel(
            f"Pinned overrides: {len(self.overrides)} (saved to {self.overrides_json.name}). Rebuild voice to apply."
        )

    def _on_save_close(self, _evt: wx.CommandEvent) -> None:
        sd.stop()
        self._save_overrides()
        self.EndModal(wx.ID_OK)

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        if dispatch_alt_mnemonic(self, evt):
            return
        code = evt.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self._on_save_close(None)
            return
        focused = wx.Window.FindFocus()
        # Pressing Enter in the Diphones list jumps straight into its Recorded Takes list
        if focused is self.list_diphones and code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.list_takes.GetItemCount() > 0:
                self.list_takes.SetFocus()
            return
        # Only bind bare single-key shortcuts when focus is inside the Recorded Takes list.
        # Never hijack Space (buttons/checkboxes need it), and never swallow letters in
        # list_diphones (so native type-ahead works) or text fields.
        if focused is not self.list_takes:
            evt.Skip()
            return
        if evt.ControlDown() or evt.AltDown() or evt.MetaDown():
            evt.Skip()
            return
        if code in (ord("W"), ord("w")):
            self._play_selected_take(diphone_only=False)
            return
        if code in (ord("D"), ord("d")):
            self._play_selected_take(diphone_only=True)
            return
        if code in (ord("R"), ord("r")):
            self._reset_current_diphone()
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._pin_selected_take()
            return
        evt.Skip()


def launch_standalone_picker(
    unit_grid_csv: Path,
    segments_csv: Path,
    audio_dir: Path,
    overrides_json: Path,
    *,
    voice: str = "en-gb",
    inventory_json: Optional[Path] = None,
) -> int:
    app = wx.App(False)
    with TakePickerDialog(
        None,
        unit_grid_csv=unit_grid_csv,
        segments_csv=segments_csv,
        audio_dir=audio_dir,
        overrides_json=overrides_json,
        voice=voice,
        inventory_json=inventory_json,
    ) as dlg:
        dlg.CenterOnScreen()
        dlg.ShowModal()
    app.Destroy()
    return 0
