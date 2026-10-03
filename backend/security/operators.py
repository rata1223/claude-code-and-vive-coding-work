"""Who is an operator: one allow-list of app user ids, read by every control
that acts on (or shows) the deployment's single ``.env`` account.

The worker trades one account. The app is multi-user and signup is open, so
being logged in proves nothing about who may sell that account's positions,
release its kill switch or watch its live feed. ``OPERATOR_USER_IDS`` names the
users who may — comma-separated ``users.id`` values (the token ``sub``).

Ids, not emails. Signup does not verify mailboxes, and the email lists this
replaces compared case-insensitively while signup's duplicate check is
case-sensitive, so a listed address protected nothing even after its owner had
registered. An id is assigned by the database and never reused.

Empty — the default — means nobody: every control that asks is dormant. That is
recoverable by setting one variable; a control everyone can fire is not.

Lives in ``backend/`` because kis-ws (``Dockerfile.kis-bot``) has no ``api/``.
"""
import logging
import os

logger = logging.getLogger(__name__)

OPERATOR_ENV = "OPERATOR_USER_IDS"

#: Variables this list replaced. Still set means the deployment expects a
#: control that now ignores them — say so at startup rather than go quiet.
LEGACY_OPERATOR_ENVS = (
    "EMERGENCY_FLATTEN_ADMINS",
    "KILL_SWITCH_ADMINS",
    "WS_OPERATOR_USER_IDS",
)


def _parse_operator_env():
    ids, rejected = set(), []
    for item in os.environ.get(OPERATOR_ENV, "").split(","):
        item = item.strip()
        if not item:
            continue
        if item.isascii() and item.isdigit() and int(item) > 0:
            ids.add(str(int(item)))
        else:
            rejected.append(item)
    return frozenset(ids), rejected


def operator_user_ids() -> frozenset:
    """The listed ids as strings. Entries that are not a positive integer are
    ignored — an email pasted here must not match anything. Read on every call
    (no cache) so tests and a restarted process see the current value; startup
    reports rejected entries once (``warn_legacy_operator_env``)."""
    return _parse_operator_env()[0]


def is_operator_id(user_id) -> bool:
    if user_id is None or isinstance(user_id, bool):
        return False
    text = str(user_id).strip()
    if not (text.isascii() and text.isdigit()):
        return False
    return str(int(text)) in operator_user_ids()


def is_operator(user) -> bool:
    """Whether an app ``User`` row is a listed operator."""
    return is_operator_id(getattr(user, "id", None))


def warn_legacy_operator_env() -> list:
    """Startup report: legacy variables still set, rejected entries, an empty
    list. Returns the legacy names found."""
    found = [name for name in LEGACY_OPERATOR_ENVS if os.environ.get(name, "").strip()]
    for name in found:
        logger.warning(
            "%s는 더 이상 읽지 않는다 — 운영자는 %s(사용자 id)로 지정한다. "
            "이 값만 있으면 해당 제어는 비활성(fail-closed)이다", name, OPERATOR_ENV)
    ids, rejected = _parse_operator_env()
    if rejected:
        # Count only: an entry that is not an id is often an email address.
        logger.warning("%s: 사용자 id가 아닌 항목 %d개 무시", OPERATOR_ENV, len(rejected))
    if not ids:
        logger.warning("%s가 비어 있음 — 운영자 제어(비상청산·킬스위치 해제·실시간 피드)는 "
                       "아무에게도 열리지 않는다", OPERATOR_ENV)
    return found
