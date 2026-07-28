# Magic Mic

**A voice assistant for Home Assistant that actually assists — and feels like magic, built like clockwork.**

Smart speakers turn your lights on and off. That's table stakes, and Home Assistant
already does it well. What they *don't* do is the stuff that makes something feel like a
real assistant: remembering the things you tell it, reminding you at the right moment in
the right room, turning a sentence into a little piece of home automation on the spot,
and holding a conversation instead of a single command.

Magic Mic is that layer. It's an LLM-backed assistant that runs *inside* Home Assistant,
multi-user from day one, local-first-friendly, and designed so the clever parts are
deterministic rather than hand-wavy. Everything below is built from the design in
[`PRODUCT_PLAN.md`](PRODUCT_PLAN.md) and the deep-dive docs in [`docs/`](docs/).

---

## The magic

### It turns a sentence into an automation

> **You:** "Remind me in an hour if I haven't closed the garage door."
>
> **Magic Mic:** "Okay — I'll check in an hour."
>
> *…an hour later, only because the door is actually still open:*
>
> **Magic Mic:** ⟨ding⟩ "You have a reminder."

No app, no blueprint, no `if/then` editor. One sentence became a real trigger, a real
condition, and a real action. If you'd closed the door, nothing would have happened — it
checked the *actual state* of the actual door, not a guess.

### Its reminders find you — without knowing where you are

Set a reminder by voice and it doesn't matter which room you wander into. When it's time,
every speaker gives a quiet, content-free nudge — *"you have a reminder"* — and reads it
out only when you ask:

> **Magic Mic:** ⟨ding⟩ "You have a reminder."
>
> **You:** "Read it."
>
> **Magic Mic:** "Water the plants."

Asking to hear it *is* the acknowledgement. Nothing private is announced to a room until
you ask for it, a reminder that goes unanswered escalates to reach you rather than
nagging one speaker forever, and anything missed while the system was down is waiting for
you the next time you speak — never blared at you three hours late.

### It learns how you talk

The first time it stumbles over you, it fixes the problem *permanently* instead of making
you repeat yourself forever:

> **You:** "Turn on the reading lamp."
>
> **Magic Mic:** "I found a couple of lamps — do you mean the one in the den?"
>
> **You:** "Yeah, that one."
>
> **Magic Mic:** "Done. Want me to call that 'the reading lamp' from now on?"
>
> **You:** "Please."

Now "the reading lamp" just works — for everyone in the house, forever. And it isn't only
names. When a whole phrase doesn't reliably land — say *"what should I wear tomorrow?"*
doesn't consistently get you the forecast, maybe because you've added other skills that
compete for it — it can offer to pin that phrase to what you actually meant, so it works
every time after. That kind of fix can even run *without the cloud at all*.

This is the whole philosophy in miniature: **it notices friction and offers a durable fix,
instead of making you adapt to it.** You always get the last word — nothing changes how it
behaves without a yes — and every fix can be taken back.

### It remembers the things you tell it

It holds the household facts a home assistant should — and no smart speaker does:

> **You:** "Remember the wifi password is hunter2-galaxy."
>
> **Magic Mic:** "Got it."
>
> *…later, from anyone in the house:*
>
> **You:** "What's the wifi password?"
>
> **Magic Mic:** "hunter2-galaxy."

Where you left the spare key, the cat's name, the car's parking spot — you tell it once, and
it's there when you ask.

### It holds a conversation

The mic stays open when it makes sense, so you can just keep talking:

> **You:** "What's playing?"
>
> **Magic Mic:** "*Tangerine* by Led Zeppelin."
>
> **You:** "What year was that recorded?"
>
> **Magic Mic:** "1970, on *Led Zeppelin III*."

No wake word between turns, and the follow-up reaches for real world knowledge when the
answer isn't in your house.

### And when you change your mind, you can take it back

> **You:** "Undo that."
>
> **Magic Mic:** "Reverted — the lights are back the way they were."

More on why that "undo" is trustworthy below.

Music that follows you from room to room, weather that actually knows tomorrow's forecast,
timers and alarms that escalate the *right* way for the situation — those round it out.
But the four moments above are the point: **a strict superset of what you already expect,
with the assistant features layered on top.**

---

## Built like clockwork

Here's the part that matters if you're the kind of person who reads a `docs/` folder for
fun: **none of this is the LLM improvising.** The experience feels magical precisely
because the machinery underneath is deliberately un-magical.

**The model decides *what* you meant; deterministic code does the *doing*.** When you say
"next Tuesday at 8," the model resolves the words into a structured date — that's a
language problem, and it's good at it — but a plain, testable function does the timezone
and calendar arithmetic. The conditional reminder above isn't the LLM "remembering to
check later"; it's a real Home Assistant trigger-and-condition, compiled once from your
sentence and then run by the same clockwork that runs every other automation in your home.
This is the load-bearing principle across the whole design: **let the LLM handle intent
and orchestration, and push every fuzzy, stateful, or safety-critical step into
deterministic tools.**

**Undo works because we capture what the LLM actually did.** "Undo that" isn't the model
reconstructing history from memory and hoping. Every action that changes something records
its own inverse *at the moment it runs* — a snapshot of the lights before they changed, the
prior value of a note, the ID of a thing that was created. "Undo" just replays those
inverses in reverse. It's a journal, not a guess, which is exactly why it's safe enough to
offer out loud — and it's what lets the assistant act *optimistically* in the first place,
because anything it does can be cleanly taken back.

**Determinism is also the road to running local.** The more of the work that lives in
crisp, deterministic tools instead of the model's head, the less the cloud is doing — and
the more can run with no cloud at all. Every capability here is built as a provider-agnostic
Home Assistant primitive, so a local model gets the same tools a cloud one does; the
difference shrinks to raw model quality. Better still, the common commands are shaped so
Home Assistant's own on-device intent matching can handle them **without ever calling the
model** — which makes those interactions faster, cheaper, private, and functional when the
internet isn't. Contributing these capabilities doesn't just serve the cloud path; it makes
the *no-AI* path better for everyone, which is the opposite of the usual "AI bolt-on"
trade-off.

---

## What makes it different

- **It's an assistant, not a command line for your house.** Memory, reminders that find
  you, conversation, conditional automations from a sentence, and a system that *learns your
  phrasing* — the things a person means when they say "assistant," not just "voice control."
- **Magical to use, boring to trust.** Deterministic tools, a real undo journal, legible
  defaults, and *no silent inference* — it never quietly changes its behavior based on a
  guess it didn't tell you about.
- **Multi-user from the first line of code.** Data is keyed per person from day one, so
  "my dentist appointment" and "the wifi password" land in the right scope even before
  the system can tell voices apart.
- **Local-first by construction, not by apology.** The design assumes people care about
  privacy and about the thing still working when the cloud doesn't — and it's engineered
  so that caring about those things costs you nothing.

---

## Where this is going

The design phase is complete — twenty deep-dive docs, a full architecture, and a
build plan. Now it gets built, in a Home Assistant custom component, on cloud Claude to
start, with every capability shaped so it can graduate into Home Assistant core and reach
everyone: fuzzy entity resolution first, then calendar writes, then persistent reminders,
then long-term memory. The shell is disposable on purpose; the capabilities are the point.

If any of this makes you want to dig in, start with [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md)
for the architecture and the [`docs/`](docs/) folder for the feature-by-feature reasoning.

*(Working name: **Magic Mic** — a nod to the internal code name for the "keep the mic
open" feature this assistant leans on, and to how it ought to feel.)*
