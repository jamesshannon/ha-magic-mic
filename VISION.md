# Magic Mic

A voice assistant for Home Assistant that does more than switch devices on and off.

Smart speakers handle "turn on the lights," and Home Assistant already does that well.
What they don't do is the rest of what you'd want from an assistant: remember the things
you tell it, remind you at the right moment in whatever room you're in, turn a spoken
sentence into a piece of home automation, and hold a back-and-forth instead of taking one
command at a time.

Magic Mic is that layer. It's an LLM-backed assistant that runs inside Home Assistant. It
keeps data separate per person from the start, runs locally where it can, and puts the
fuzzy language work in the model while keeping the state changes in plain, testable code.
The design behind it is in [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md), with feature-by-feature
detail in [`docs/`](docs/).

---

## What it does

### Turn a sentence into an automation

> **You:** "Remind me in an hour if I haven't closed the garage door."
>
> **Magic Mic:** "Okay, I'll check in an hour."
>
> *…an hour later, only because the door is still open:*
>
> **Magic Mic:** ⟨ding⟩ "You have a reminder."

No app, no blueprint, no `if/then` editor. One sentence becomes a real trigger, a real
condition, and a real action. Close the door before the hour is up and nothing fires,
because the check reads the door's actual state.

### Reminders that reach you in any room

> **Magic Mic:** ⟨ding⟩ "You have a reminder."
>
> **You:** "Read it."
>
> **Magic Mic:** "Water the plants."

Set a reminder by voice and it doesn't matter where you wander. When it's due, a nearby
speaker gives a short, content-free nudge, *"you have a reminder,"* and reads it out only
when you ask. If it is ignored, the nudge can expand to other rooms instead of nagging one
speaker forever. Asking to hear it counts as the acknowledgement. Nothing private gets
spoken into a room until policy allows it, and anything that came due while the system was
offline waits for the next interaction rather than arriving three hours late.

### Learns how you talk

The first time it stumbles over you, it fixes the problem for good instead of making you
repeat yourself:

> **You:** "Turn on the reading lamp."
>
> **Magic Mic:** "I found a couple of lamps. Do you mean the one in the den?"
>
> **You:** "Yeah, that one."
>
> **Magic Mic:** "Done. Want me to call that 'the reading lamp' from now on?"
>
> **You:** "Please."

Now "the reading lamp" works for everyone in the house. It isn't only names. When a whole
phrase stops landing reliably (say *"what should I wear tomorrow?"* stops reaching the
forecast because another skill you added now competes for it), it can offer to pin that
phrase to what you meant, so it works every time after. That kind of fix can run without
the cloud at all.

This is the whole approach in miniature: it notices friction and offers a fix you can
accept or decline, rather than making you adapt to it. You always get the last word,
nothing changes how it behaves without a yes, and every fix can be taken back.

### Remembers what you tell it

It holds the household facts a home assistant should, and no smart speaker does:

> **You:** "Remember the wifi password is hunter2-galaxy."
>
> **Magic Mic:** "Got it."
>
> *…later, from anyone in the house:*
>
> **You:** "What's the wifi password?"
>
> **Magic Mic:** "hunter2-galaxy."

Where you left the spare key, the cat's name, where the car is parked: tell it once, ask
any time.

### Holds a conversation

The mic stays open when it makes sense, so you can keep talking:

> **You:** "What's playing?"
>
> **Magic Mic:** "*Tangerine* by Led Zeppelin."
>
> **You:** "What year was that recorded?"
>
> **Magic Mic:** "1970, on *Led Zeppelin III*."

No wake word between turns, and the follow-up reaches for real-world knowledge when the
answer isn't in your house.

### Tells you why something happened

> **You:** "Why did the hallway light just turn on?"
>
> **Magic Mic:** "Your Hallway Motion automation turned it on a moment ago."

Home Assistant already records what caused each change in your house. Magic Mic reads that
record and answers in plain language, so a light that surprised you gets a reason instead of
a shrug. And when nothing recorded a cause, say a thermostat that followed its own schedule
in the cloud, it tells you that rather than making something up. A guess dressed as an answer
would be worse than admitting it doesn't know.

### Takes it back when you change your mind

> **You:** "Undo that."
>
> **Magic Mic:** "Reverted. The lights are back the way they were."

More on why that undo is trustworthy below.

Music that follows you between rooms, weather that knows tomorrow's forecast, and timers and
alarms that escalate to fit the situation round it out. So do the basics you'd expect from
any smart speaker, and a few of those come with a twist: white noise and sleep sounds, for
one, play straight from the device with no cloud stream and no subscription, so they don't
stop when the internet does. The moments above are the point; the rest just works.

---

## How it stays reliable

Here's the part that matters if you're the kind of person who reads a `docs/` folder for
fun: none of this is the LLM improvising. It feels smooth because the machinery underneath
is deliberately plain.

The model decides *what* you meant; ordinary code does the *doing*. Say "next Tuesday at
8" and the model turns the words into a structured date, which is a language problem it's
good at. A plain, testable function then does the timezone and calendar arithmetic. The
conditional reminder above isn't the model remembering to check later; it's a real Home
Assistant trigger and condition, compiled once from your sentence and run by the same
machinery that runs every other automation in your home. This holds across the design: the
LLM handles intent and orchestration, and every fuzzy, stateful, or safety-critical step
goes into a deterministic tool.

Undo works because an undo-capable action records what it actually did. "Undo that" doesn't
reconstruct history from the model's memory and hope. Every action that promises undo
writes its own inverse the moment it runs: a snapshot of the lights before they changed,
the previous value of a note, the ID of a thing that was created. Undo replays those
inverses in reverse. It's a journal rather than a reconstruction, which is why it's safe to
offer out loud, and it's what lets the assistant act right away, since anything it does can
be cleanly reversed.

Determinism is also how more of this can run locally. The more work that lives in deterministic tools
instead of the model's head, the less the cloud has to do, and the more can run with no
cloud at all. Provider-specific transport stays out of the deterministic capability logic,
so local and cloud models can use the same underlying behavior and differ mainly in model
quality. The common commands also use Home Assistant's on-device intent matching
can handle them without calling the model, which makes those interactions faster, cheaper,
private, and usable with no internet. Contributing these capabilities improves the no-AI
path too, not just the cloud one.

---

## What makes it different

- **An assistant, not a remote control for your house.** Memory, reminders that reach you,
  conversation, conditional automations from a sentence, and phrasing it learns: the things
  people mean by "assistant," not just voice control.
- **Fun to use, dull to trust.** Deterministic tools, a real undo journal, plain defaults,
  and no silent inference. It won't quietly change how it behaves on a guess it didn't tell
  you about.
- **Multi-user from the first line of code.** Data records household or personal scope from
  the start, so "my dentist appointment" and "the wifi password" have different audiences
  before the system can even tell voices apart.
- **Local-first by construction.** The design assumes people care about privacy and about
  the thing still working when the cloud doesn't, and it's built so that caring costs you
  nothing.

---

## Where this is going

The design is developed enough to build and test as one Home Assistant custom-component
proving ground, running on cloud Claude to start. The goal is evidence: demonstrate the
experience, measure it, expose the missing Assist seams, and carry successful behavior,
contracts, schemas, and tests into architecture discussions with Home Assistant maintainers.
That will require adapting the work to core, not copying a folder of pre-shaped integrations.

To dig in, start with [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md) for the architecture and the
[`docs/`](docs/) folder for the feature-by-feature reasoning.

*(Working name: Magic Mic, after the internal code name for the "keep the mic open"
feature the assistant leans on.)*
