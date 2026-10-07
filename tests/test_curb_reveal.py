"""Names in the web UI: one browser, five minutes, after a yes to the page's code (§11.3)."""

import pytest

from flanner import curb_approval, curb_reveal


class Prompt:
    name, weak, shows_reason = "test prompt", False, True

    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def available(self):
        return True

    def confirm(self, reason):
        self.asked.append(reason)
        return self.answer


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    now = [1000.0]
    return now


def reveals(clock):
    return curb_reveal.Reveals(clock=lambda: clock[0])


def test_a_yes_to_the_code_shows_names_for_five_minutes(clock):
    held, prompt = reveals(clock), Prompt()
    browser = held.new_token()
    code = held.code(browser)
    assert len(code) == 4 and held.seconds_left(browser) == 0
    held.show(browser, curb_approval.Broker(prompt))
    assert f"Code {code}" in prompt.asked[0]
    assert held.seconds_left(browser) == curb_reveal.REVEAL_SECONDS
    clock[0] += curb_reveal.REVEAL_SECONDS + 1
    assert held.seconds_left(browser) == 0


def test_only_the_browser_that_asked_sees_names(clock):
    held = reveals(clock)
    mine, other = held.new_token(), held.new_token()
    held.code(mine)
    held.show(mine, curb_approval.Broker(Prompt()))
    assert held.seconds_left(mine) and not held.seconds_left(other)
    assert not held.seconds_left(None) and not held.seconds_left("made-up")


def test_no_two_waiting_reveals_share_a_code(clock):
    held = reveals(clock)
    codes = [held.code(held.new_token()) for _ in range(curb_reveal.MAX_WAITING)]
    assert len(set(codes)) == len(codes)


def test_a_refusal_an_old_code_or_no_code_shows_nothing(clock):
    held = reveals(clock)
    browser = held.new_token()
    with pytest.raises(curb_reveal.NotShown, match="ask again"):
        held.show(browser, curb_approval.Broker(Prompt()))
    held.code(browser)
    with pytest.raises(curb_reveal.NotShown, match="not approved"):
        held.show(browser, curb_approval.Broker(Prompt(answer=False)))
    held.code(browser)
    clock[0] += curb_reveal.CODE_SECONDS + 1
    with pytest.raises(curb_reveal.NotShown, match="expired"):
        held.show(browser, curb_approval.Broker(Prompt()))
    assert held.seconds_left(browser) == 0


def test_a_prompt_that_cannot_show_the_code_is_never_asked(clock):
    held, prompt = reveals(clock), Prompt()
    prompt.shows_reason = False
    browser = held.new_token()
    held.code(browser)
    with pytest.raises(curb_reveal.NotShown, match="cannot show the code"):
        held.show(browser, curb_approval.Broker(prompt))
    assert prompt.asked == []
    assert curb_reveal.unavailable(None) == "no approval method on this machine"


def test_hiding_ends_a_reveal_at_once(clock):
    held = reveals(clock)
    browser = held.new_token()
    held.code(browser)
    held.show(browser, curb_approval.Broker(Prompt()))
    held.hide(browser)
    assert held.seconds_left(browser) == 0
