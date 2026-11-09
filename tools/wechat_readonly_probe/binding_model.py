"""Synthetic identity-consistency model. Never an authorization decision.

All observations are supplied by the caller and can be forged. This deliberately
contains no Windows APIs, database access, or way to open a user process.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Process:
    pid: int
    created: int
    signature_verified: bool


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    process_created: int
    visible_stable_account_id: str


@dataclass(frozen=True)
class FileIdentity:
    volume: int
    file_id: int


@dataclass(frozen=True)
class DatabaseObservation:
    holder_pid: int
    holder_created: int
    root_id: FileIdentity
    self_db_id: FileIdentity
    target_db_id: FileIdentity
    self_db_account_id: str
    root_membership_verified: bool
    process_holds_expected_files: bool
    account_from_independent_record: bool


def synthetic_consistent(
    process_before: Process,
    window_before: Window,
    db_before: DatabaseObservation,
    process_after: Process,
    window_after: Window,
    db_after: DatabaseObservation,
) -> bool:
    """Check mock consistency only; True never authorizes real access."""
    if not (type(process_before) is Process and type(process_after) is Process
            and type(window_before) is Window and type(window_after) is Window
            and type(db_before) is DatabaseObservation and type(db_after) is DatabaseObservation):
        return False
    if process_before != process_after or window_before != window_after or db_before != db_after:
        return False
    p, w, d = process_before, window_before, db_before
    if (type(p.pid) is not int or p.pid <= 0 or type(p.created) is not int
            or p.created <= 0 or p.signature_verified is not True):
        return False
    if (type(w.hwnd) is not int or w.hwnd <= 0 or w.pid != p.pid
            or w.process_created != p.created):
        return False
    if (type(d.holder_pid) is not int or d.holder_pid != p.pid
            or d.holder_created != p.created):
        return False
    ids = (d.root_id, d.self_db_id, d.target_db_id)
    if any(type(x) is not FileIdentity or type(x.volume) is not int
           or type(x.file_id) is not int or x.volume < 0 or x.file_id <= 0 for x in ids):
        return False
    if len(set(ids)) != len(ids) or len({x.volume for x in ids}) != 1:
        return False
    if any(flag is not True for flag in (d.root_membership_verified,
                                          d.process_holds_expected_files,
                                          d.account_from_independent_record)):
        return False
    account = w.visible_stable_account_id
    if type(account) is not str or not account.startswith("wxid_") or len(account) <= 5:
        return False
    return account == d.self_db_account_id
