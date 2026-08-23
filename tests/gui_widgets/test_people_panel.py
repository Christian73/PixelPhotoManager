# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour people_panel — helpers
de vignettes de visage, dialogues d'assignation/fusion, lignes de groupe et
PeopleDialog sur des FaceDatabase/Catalog réels semés en process. Les dialogues
ne sont jamais exec() : on pilote leurs méthodes directement."""
import math
import sqlite3

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QLabel

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _ClusterRow, MergePersonsDialog, PeopleDialog,
    _AVATAR_SIZE, _cosine_sim, _face_bytes, _load_edit_rotations,
    _placeholder_pixmap, load_face_pixmap,
)


def _make_photo(path, w=200, h=160) -> str:
    Image.new("RGB", (w, h), color=(120, 100, 80)).save(path)
    return str(path)


def _face(photo_path, x=20, y=20, w=60, h=60, rot=0) -> FaceInfo:
    return FaceInfo(
        id=1, photo_path=str(photo_path),
        bbox_x=x, bbox_y=y, bbox_w=w, bbox_h=h,
        detected_rotation=rot,
    )


def _person(pid, name, count=3) -> PersonInfo:
    return PersonInfo(name=name, id=pid, photo_count=count)


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    suggestion_person_id=None, bbox=(10, 10, 50, 50),
    embedding=None, is_cover=0,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            "  cluster_id, person_id, suggestion_person_id, embedding, is_cover)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                photo_path, *bbox, cluster_id, person_id, suggestion_person_id,
                _enc(embedding) if embedding is not None else None, is_cover,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _tilted_embedding(angle_rad: float, dim: int = 8) -> list[float]:
    vec = [0.0] * dim
    vec[0] = math.cos(angle_rad)
    vec[1] = math.sin(angle_rad)
    return vec


# ---------------------------------------------------------------------------
# helpers purs

class TestCosineSim:
    def test_identical_vectors(self):
        assert _cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-6)

    def test_zero_vector_returns_zero(self):
        assert _cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestLoadEditRotations:
    def test_empty_input(self):
        assert _load_edit_rotations([]) == {}

    def test_missing_db_returns_empty(self, tmp_path, monkeypatch):
        import src.core.app_dirs as app_dirs
        monkeypatch.setattr(app_dirs, "APP_DATA_DIR", tmp_path / "nulle_part")
        assert _load_edit_rotations(["C:/x.jpg"]) == {}

    def test_reads_nonzero_rotations(self, tmp_path, monkeypatch):
        import src.core.app_dirs as app_dirs
        monkeypatch.setattr(app_dirs, "APP_DATA_DIR", tmp_path)
        con = sqlite3.connect(tmp_path / "edits.db")
        con.execute("CREATE TABLE photo_edits (photo_path TEXT, rotation REAL)")
        con.executemany(
            "INSERT INTO photo_edits VALUES (?, ?)",
            [("C:/a.jpg", 90.0), ("C:/b.jpg", 0.0), ("C:/c.jpg", 270.0)],
        )
        con.commit()
        con.close()

        rots = _load_edit_rotations(["C:/a.jpg", "C:/b.jpg", "C:/c.jpg"])

        # rotation nulle filtrée : seule une rotation effective est retournée
        assert rots == {"C:/a.jpg": 90, "C:/c.jpg": 270}


class TestFaceBytes:
    def test_valid_face_returns_png(self, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg")

        data = _face_bytes(_face(photo), size=48)

        assert data.startswith(b"\x89PNG")
        with Image.open(__import__("io").BytesIO(data)) as img:
            assert img.size == (48, 48)

    def test_detected_rotation_applied(self, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg")

        data = _face_bytes(_face(photo, rot=90), size=48, edit_rotation=90)

        assert data.startswith(b"\x89PNG")

    def test_video_extension_returns_empty(self):
        assert _face_bytes(_face("C:/clip.mp4"), size=48) == b""

    def test_degenerate_bbox_returns_empty(self, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg", w=50, h=50)

        # bbox entièrement hors image → crop vide
        assert _face_bytes(_face(photo, x=500, y=500), size=48) == b""

    def test_missing_file_returns_empty(self):
        assert _face_bytes(_face("C:/nulle/part.jpg"), size=48) == b""


class TestLoadFacePixmap:
    def test_valid_face(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg")

        pix = load_face_pixmap(_face(photo), size=48)

        assert not pix.isNull()
        assert pix.width() == 48

    def test_fallback_gray_pixmap_on_failure(self, qtbot):
        pix = load_face_pixmap(_face("C:/nulle/part.jpg"), size=32)

        assert not pix.isNull()
        assert pix.width() == 32

    def test_placeholder_pixmap(self, qtbot):
        pix = _placeholder_pixmap(24)
        assert (pix.width(), pix.height()) == (24, 24)


class TestAvatarLoader:
    def test_run_emits_bytes_per_valid_item(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg")
        items = [(1, _face(photo)), (2, _face("C:/absent.jpg"))]
        loader = _AvatarLoader(items, size=32)
        results = []
        loader.avatar_ready.connect(lambda cid, data: results.append(cid))

        loader.run()   # synchrone : tracé par coverage

        assert results == [1]   # le visage illisible n'émet rien


# ---------------------------------------------------------------------------
# dialogue d'assignation

class TestAssignDialog:
    def _persons(self):
        return [_person(1, "Alice", 5), _person(2, "Boris", 1), _person(3, "Chloé")]

    def test_suggestion_preselected_and_accept(self, qtbot):
        dlg = _AssignDialog(7, self._persons(), suggested_person_id=2)
        qtbot.addWidget(dlg)

        checked = dlg._btn_group.checkedButton()
        assert checked.property("person_id") == 2
        assert "Boris" in checked.text() and "(1 photo)" in checked.text()

        dlg._on_accept()
        assert dlg.result() == QDialog.Accepted
        assert not dlg.is_new_person()
        assert dlg.existing_person_id() == 2

    def test_filter_hides_and_moves_selection(self, qtbot):
        dlg = _AssignDialog(7, self._persons())
        qtbot.addWidget(dlg)
        assert dlg._btn_group.checkedButton().property("person_id") == 1

        dlg._search_input.setText("chlo")

        visible = [name for rb, name in dlg._person_rbs if not rb.isHidden()]
        assert visible == ["Chloé"]
        assert dlg._btn_group.checkedButton().property("person_id") == 3

    def test_filter_no_match_falls_back_to_new(self, qtbot):
        dlg = _AssignDialog(7, self._persons())
        qtbot.addWidget(dlg)

        dlg._search_input.setText("zzz")

        assert dlg._btn_group.checkedButton() is dlg._rb_new

    def test_new_person_requires_name(self, qtbot):
        dlg = _AssignDialog(7, [])
        qtbot.addWidget(dlg)
        assert dlg._btn_group.checkedButton() is dlg._rb_new

        dlg._on_accept()                       # nom vide → refus silencieux
        assert dlg.result() != QDialog.Accepted

        dlg._name_input.setText("Zoé")
        dlg._on_accept()
        assert dlg.result() == QDialog.Accepted
        assert dlg.is_new_person()
        assert dlg.new_name() == "Zoé"

    def test_typing_name_checks_new_radio(self, qtbot):
        dlg = _AssignDialog(7, self._persons())
        qtbot.addWidget(dlg)
        assert dlg._btn_group.checkedButton() is not dlg._rb_new

        dlg._name_input.setText("Quelqu'un")

        assert dlg._btn_group.checkedButton() is dlg._rb_new

    def test_ignore_option(self, qtbot):
        dlg = _AssignDialog(7, self._persons(), show_ignore=True)
        qtbot.addWidget(dlg)
        dlg._rb_ignore.setChecked(True)

        dlg._on_accept()

        assert dlg.result() == QDialog.Accepted
        assert dlg.is_ignored()
        assert not dlg.is_new_person()

    def test_no_ignore_radio_when_disabled(self, qtbot):
        dlg = _AssignDialog(7, self._persons(), show_ignore=False)
        qtbot.addWidget(dlg)
        assert dlg._rb_ignore is None


# ---------------------------------------------------------------------------
# dialogue de fusion

class TestMergePersonsDialog:
    def test_lists_others_and_accepts_target(self, qtbot):
        source = _person(1, "Alice")
        others = [source, _person(2, "Boris"), _person(3, "Chloé")]
        dlg = MergePersonsDialog(source, others)
        qtbot.addWidget(dlg)

        names = [name for _, name in dlg._person_rbs]
        assert names == ["Boris", "Chloé"]           # source exclue
        assert dlg._btn_group.checkedButton() is not None

        dlg._on_accept()
        assert dlg.result() == QDialog.Accepted
        assert dlg.target_person_id() == 2

    def test_filter_moves_selection(self, qtbot):
        source = _person(1, "Alice")
        dlg = MergePersonsDialog(source, [source, _person(2, "Boris"), _person(3, "Chloé")])
        qtbot.addWidget(dlg)

        dlg._search_input.setText("chl")

        assert dlg._btn_group.checkedButton().property("person_id") == 3

    def test_no_other_person(self, qtbot):
        source = _person(1, "Alice")
        dlg = MergePersonsDialog(source, [source])
        qtbot.addWidget(dlg)

        assert dlg.target_person_id() is None
        assert not hasattr(dlg, "_search_input")


# ---------------------------------------------------------------------------
# ligne de groupe

class TestClusterRow:
    def test_strong_suggestion_label(self, qtbot):
        row = _ClusterRow(5, 3, None, [], suggestion=("Alice", 1, 0.62))
        qtbot.addWidget(row)

        labels = [lbl.text() for lbl in row.findChildren(QLabel)]
        assert any("Probablement Alice (62 %)" in t for t in labels)

    def test_weak_suggestion_label(self, qtbot):
        row = _ClusterRow(5, 1, None, [], suggestion=("Boris", 2, 0.46))
        qtbot.addWidget(row)

        labels = [lbl.text() for lbl in row.findChildren(QLabel)]
        assert any("Peut-être Boris (46 %)" in t for t in labels)
        assert any("Isolated" in t for t in labels)   # face_count == 1

    def test_set_avatar(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path / "p.jpg")
        row = _ClusterRow(5, 2, None, [])
        qtbot.addWidget(row)

        row.set_avatar(_face_bytes(_face(photo), _AVATAR_SIZE))

        assert row._lbl_avatar.pixmap() is not None
        assert not row._lbl_avatar.pixmap().isNull()

    def _patch_assign_dialog(self, monkeypatch, *, ignored=False,
                             new_person=False, new_name="", person_id=None):
        monkeypatch.setattr(_AssignDialog, "exec", lambda self: QDialog.Accepted)
        monkeypatch.setattr(_AssignDialog, "is_ignored", lambda self: ignored)
        monkeypatch.setattr(_AssignDialog, "is_new_person", lambda self: new_person)
        monkeypatch.setattr(_AssignDialog, "new_name", lambda self: new_name)
        monkeypatch.setattr(_AssignDialog, "existing_person_id", lambda self: person_id)

    def test_ask_name_emits_named_for_new_person(self, qtbot, monkeypatch):
        row = _ClusterRow(5, 2, None, [])
        qtbot.addWidget(row)
        self._patch_assign_dialog(monkeypatch, new_person=True, new_name="Zoé")

        with qtbot.waitSignal(row.named, timeout=1000) as blocker:
            row._ask_name()

        assert blocker.args == [5, "Zoé"]

    def test_ask_name_emits_assigned_for_existing(self, qtbot, monkeypatch):
        row = _ClusterRow(5, 2, None, [])
        qtbot.addWidget(row)
        self._patch_assign_dialog(monkeypatch, person_id=42)

        with qtbot.waitSignal(row.assigned, timeout=1000) as blocker:
            row._ask_name()

        assert blocker.args == [5, 42]

    def test_ask_name_emits_ignored(self, qtbot, monkeypatch):
        row = _ClusterRow(5, 2, None, [])
        qtbot.addWidget(row)
        self._patch_assign_dialog(monkeypatch, ignored=True)

        with qtbot.waitSignal(row.ignored, timeout=1000) as blocker:
            row._ask_name()

        assert blocker.args == [5]

    def test_ask_name_rejected_emits_nothing(self, qtbot, monkeypatch):
        row = _ClusterRow(5, 2, None, [])
        qtbot.addWidget(row)
        monkeypatch.setattr(_AssignDialog, "exec", lambda self: QDialog.Rejected)
        fired = []
        row.named.connect(lambda *a: fired.append("named"))
        row.assigned.connect(lambda *a: fired.append("assigned"))
        row.ignored.connect(lambda *a: fired.append("ignored"))

        row._ask_name()

        assert fired == []


# ---------------------------------------------------------------------------
# PeopleDialog

def _wait_avatar_loader(qtbot, dlg) -> None:
    # Polling plutôt que waitSignal(finished) : le thread peut se terminer entre
    # le test isRunning() et le branchement du signal (émission ratée → timeout).
    loader = getattr(dlg, "_avatar_loader", None)
    if loader is None:
        return

    def _done():
        try:
            return not loader.isRunning()
        except RuntimeError:
            return True   # deleteLater déjà passé

    qtbot.waitUntil(_done, timeout=3000)


class TestPeopleDialog:
    def _make(self, qtbot, tmp_path, seed=None):
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        if seed:
            seed(face_db, catalog)
        dlg = PeopleDialog(face_db, catalog)
        qtbot.addWidget(dlg)
        _wait_avatar_loader(qtbot, dlg)
        return dlg, face_db, catalog

    def test_no_cluster_shows_done_message(self, qtbot, tmp_path):
        dlg, _, _ = self._make(qtbot, tmp_path)

        labels = [lbl.text() for lbl in dlg._content.findChildren(QLabel)]
        assert any("Tous les groupes ont été nommés" in t for t in labels)
        assert dlg._rows == {}

    def test_unnamed_clusters_build_rows_with_suggestion(self, qtbot, tmp_path):
        photo_holder = {}

        def seed(face_db, catalog):
            photo = _make_photo(tmp_path / "p.jpg")
            photo_holder["p"] = photo
            alice = catalog.create_person("Alice")
            # Visage identifié d'Alice (cluster 99) — source du centroïde
            _raw_insert_face(face_db, photo, cluster_id=99, person_id=alice.id,
                             embedding=_tilted_embedding(0.0))
            # Groupe anonyme 1, similarité ~0.60 avec Alice → suggestion
            _raw_insert_face(face_db, photo, cluster_id=1,
                             embedding=_tilted_embedding(math.acos(0.60)))
            _raw_insert_face(face_db, photo, cluster_id=1,
                             embedding=_tilted_embedding(math.acos(0.60)))

        dlg, _, _ = self._make(qtbot, tmp_path, seed)

        assert list(dlg._rows.keys()) == [1]
        labels = [lbl.text() for lbl in dlg._rows[1].findChildren(QLabel)]
        assert any("Probablement Alice" in t for t in labels)
        assert any("2 visages" in t for t in labels)

    def test_on_named_emits_and_refreshes(self, qtbot, tmp_path):
        def seed(face_db, catalog):
            photo = _make_photo(tmp_path / "p.jpg")
            _raw_insert_face(face_db, photo, cluster_id=1,
                             embedding=_tilted_embedding(0.0))

        dlg, _, _ = self._make(qtbot, tmp_path, seed)

        with qtbot.waitSignal(dlg.cluster_named, timeout=1000) as blocker:
            dlg._on_named(1, "Zoé")

        assert blocker.args == [1, "Zoé"]
        _wait_avatar_loader(qtbot, dlg)

    def test_on_assigned_emits_and_refreshes(self, qtbot, tmp_path):
        def seed(face_db, catalog):
            photo = _make_photo(tmp_path / "p.jpg")
            _raw_insert_face(face_db, photo, cluster_id=1,
                             embedding=_tilted_embedding(0.0))

        dlg, _, _ = self._make(qtbot, tmp_path, seed)

        with qtbot.waitSignal(dlg.cluster_assigned, timeout=1000) as blocker:
            dlg._on_assigned(1, 42)

        assert blocker.args == [1, 42]
        _wait_avatar_loader(qtbot, dlg)

    def test_on_ignored_removes_cluster(self, qtbot, tmp_path):
        def seed(face_db, catalog):
            photo = _make_photo(tmp_path / "p.jpg")
            _raw_insert_face(face_db, photo, cluster_id=1,
                             embedding=_tilted_embedding(0.0))

        dlg, face_db, _ = self._make(qtbot, tmp_path, seed)
        assert 1 in dlg._rows

        dlg._on_ignored(1)

        assert dlg._rows == {}
        assert face_db.get_unnamed_clusters() == []
        _wait_avatar_loader(qtbot, dlg)
