"""Login state, and turning a manually supplied session value into cookies.

Platform-agnostic on purpose: the adapters own *how* a session is validated
(their own official endpoint), this module owns the vocabulary the GUI renders
and the parsing of the one thing the user may have to paste by hand.
"""

from __future__ import annotations

import http.cookiejar
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard

from core.exceptions import SessionValueError
from core.models import Platform
from core.session_store import make_cookie

logger = logging.getLogger(__name__)

#: Refuse anything implausibly long; a session cookie is a few hundred bytes.
MAX_SESSION_TEXT = 8192


class LoginState(StrEnum):
    """What the application believes about a platform session."""

    LOGGED_OUT = "logged_out"
    LOGGED_IN = "logged_in"
    #: A session file exists but the platform rejected it.
    EXPIRED = "expired"
    #: The platform could not be reached, so nothing can be concluded.
    UNKNOWN = "unknown"


#: State -> the label shown next to the platform name.
STATE_LABELS: dict[LoginState, str] = {
    LoginState.LOGGED_OUT: "未登录",
    LoginState.LOGGED_IN: "已登录",
    LoginState.EXPIRED: "登录已过期",
    LoginState.UNKNOWN: "状态未知",
}

#: State -> the label of the single action button beside it.
ACTION_LABELS: dict[LoginState, str] = {
    LoginState.LOGGED_OUT: "登录",
    LoginState.LOGGED_IN: "退出登录",
    LoginState.EXPIRED: "重新登录",
    LoginState.UNKNOWN: "登录",
}


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """The verdict for one platform."""

    platform: Platform
    state: LoginState
    #: Account name reported by the platform, when it gives one.
    account: str | None = None
    #: Short, safe explanation (never contains a cookie value).
    detail: str = ""

    @property
    def logged_in(self) -> bool:
        return self.state is LoginState.LOGGED_IN

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state.value)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.state, "登录")

    def summary(self) -> str:
        """One-line description for the settings row."""

        label = self.state_label
        if self.logged_in and self.account:
            return f"{label}（{self.account}）"
        return label


def header_has_cookie(header: str | None, names: tuple[str, ...]) -> TypeGuard[str]:
    """True when ``header`` carries at least one of ``names``.

    Used as a precondition before trusting a platform's "yes" answer: a cookie
    header without the platform's authentication cookie is not a session, no
    matter what an endpoint happens to return for an anonymous caller.

    Typed as a :class:`~typing.TypeGuard` so a caller that passes the check also
    gets ``str | None`` narrowed to ``str``.
    """

    if not header or not names:
        return False
    present = {part.split("=", 1)[0].strip() for part in header.split(";") if "=" in part}
    return bool(present & set(names))


def parse_session_text(
    text: str,
    *,
    cookie_names: tuple[str, ...],
    domain: str,
    secure: bool = True,
) -> list[http.cookiejar.Cookie]:
    """Turn pasted text into cookies for ``domain``.

    Two shapes are accepted, because both are things a user can reasonably copy:

    * a whole ``Cookie:`` header - ``SESSDATA=...; bili_jct=...``;
    * just the value of the platform's primary cookie - ``abc%2Cdef``.

    At least one of ``cookie_names`` must be present, otherwise the text is
    almost certainly the wrong value and saying so is more useful than writing a
    session file that will never authenticate. Every parsed pair is kept so a
    full header does not lose the auxiliary cookies the platform also wants.
    """

    cleaned = (text or "").strip()
    if not cleaned:
        raise SessionValueError("会话内容为空", detail="请粘贴 Cookie 头或会话值。")
    if len(cleaned) > MAX_SESSION_TEXT:
        raise SessionValueError(
            "会话内容过长",
            detail=(
                f"粘贴的内容超过 {MAX_SESSION_TEXT} 字符，请确认复制的是 Cookie 而不是整页内容。"
            ),
        )
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[len("cookie:") :].strip()

    pairs: list[tuple[str, str]] = []
    if "=" in cleaned:
        for chunk in cleaned.replace("\n", ";").split(";"):
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name = name.strip()
            if name:
                pairs.append((name, value.strip()))
    elif cookie_names:
        pairs.append((cookie_names[0], cleaned))

    if not pairs:
        raise SessionValueError(
            "无法解析会话内容",
            detail="请粘贴形如 SESSDATA=... 的 Cookie 头，或该 Cookie 的值本身。",
        )

    present = {name for name, _ in pairs}
    expected = set(cookie_names)
    if expected and not (present & expected):
        raise SessionValueError(
            "会话内容里没有该平台的登录 Cookie",
            detail=(
                f"需要包含 {', '.join(cookie_names)} 之一，"
                f"实际拿到的是：{', '.join(sorted(present))}"
            ),
        )

    return [make_cookie(name, value, domain=domain, secure=secure) for name, value in pairs]
