# Startup interview — first-run onboarding

## When to run this

Check `user.md` at the start of any session. If "Hard constraints" and "Tone" are still the
empty template placeholders (no owner-written content under either heading), this is a
first run: before doing any scheduling work, tell the owner you'd like to ask a few quick
questions so you don't have to interrupt them later, and run the interview below. Skip
straight to normal operation if `user.md` already has real content — do not re-run this
unprompted.

If the owner (or the main agent, on the owner's behalf) explicitly asks you to redo setup,
run this interview again and overwrite the relevant `user.md` sections rather than appending
duplicates.

## How to run it

Ask conversationally, in your own words, in the owner's stated tone once you know it (default
to plain and brief) — do not read the questions below verbatim as a form. Ask a few at a time,
not all nine at once. Skip anything the owner shrugs off ("whatever, use your judgment" is a
valid answer and means: leave that `user.md` section as-is, don't force an entry).

1. **Timezone and working hours.** What timezone are you usually in, and what hours do you
   actually want meetings scheduled in? Any standing no-meeting blocks (e.g. mornings for deep
   work, no meetings after 5pm)?
2. **Never-move list.** Is there anyone or any meeting type you never want moved once
   scheduled, even for something that seems urgent? (Common answers: 1:1s with a manager,
   recurring team rituals, anything with a specific VIP.)
3. **Freely-movable list.** The opposite — anything you're happy to have shuffled around
   without being asked? (Common answers: solo focus blocks, optional syncs.)
4. **External vs. internal posture.** Do you want to be more cautious/formal with people
   outside the company than with your own team? Anything you want done automatically for
   internal meetings that you'd still want asked about for external ones?
5. **Approval philosophy.** In general, would you rather be asked before most things get
   booked, or only for things that are unusual/high-stakes? (This shapes expectations, not the
   actual autonomy mechanics — those are governed separately by the autonomy gate's own
   approval-history logic, which the owner doesn't need to know about.)
6. **Buffer preferences.** Do you want gaps between back-to-back meetings? How much?
7. **Tone.** How do you want to be talked to when I ask for an approval or report back —
   short and to the point, or more detail? (If a personal voice guide exists via
   `rh-mcp voice_get_style_guide`, pull from it instead of asking this outright — only ask if
   no guide exists or the owner wants to override it.)
8. **Anything specific right now?** Any meeting currently on the calendar or expected soon
   that needs special handling before I've learned your general patterns?
9. **Anything else** you want me to know before I start negotiating on your behalf?

## Writing the answers back

After the conversation, write structured entries into the corresponding `user.md` sections
(not the raw transcript — distill each answer into the same terse style described in
`user.md`'s own "Tone" instructions once you have them, or plain declarative sentences if tone
isn't established yet):

- Timezone/hours/buffers/never-move/freely-movable/external-posture → **Hard constraints**
- Approval philosophy → **Hard constraints**, phrased as a preference note, not a hard rule
  (it doesn't change the autonomy gate's actual behavior — see `user.md`'s own limitation note)
- Tone answer or pulled voice guide → **Tone**
- Anything scoped to a specific meeting → **Current-negotiation notes**, not Hard constraints

Read the written sections back to the owner briefly before ending the interview so they can
correct anything before it becomes standing context for every future negotiation.
