"""1.43.2 — one missed holdings read does not delete a live name."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import blotter_reconcile as br


def test_first_miss_holds_the_name():
    drop, pending = br.confirm_ghost_drops(["F"], set())
    assert drop == []
    assert pending == {"F"}


def test_second_miss_drops_it():
    drop, pending = br.confirm_ghost_drops(["F"], {"F"})
    assert drop == ["F"]
    assert pending == set()


def test_name_that_comes_back_is_cleared():
    drop, pending = br.confirm_ghost_drops([], {"F"})
    assert drop == []
    assert pending == set()


def test_dark_book_does_not_count_as_a_read():
    drop, pending = br.confirm_ghost_drops(["F"], set(), book_is_live=False)
    assert drop == []
    assert pending == set()
    drop, pending = br.confirm_ghost_drops(
        ["F"], {"F"}, book_is_live=False,
    )
    assert drop == []
    assert pending == {"F"}
