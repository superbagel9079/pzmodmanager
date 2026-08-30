```
                               _      _               _
                              | |    | |             | |
  _ __ _____ __ ___   ___   __| | ___| |__   ___  ___| | __
 | '_ \_  / '_ ` _ \ / _ \ / _` |/ __| '_ \ / _ \/ __| |/ /
 | |_) / /| | | | | | (_) | (_| | (__| | | |  __/ (__|   <
 | .__/___|_| |_| |_|\___/ \__,_|\___|_| |_|\___|\___|_|\_\
 | |
 |_|
```

by superbagel9079

Scans the Project Zomboid mods installed on your machine and reports what
overlaps: missing dependencies, overwritten files, redefined script objects.

Client-side tool. Targets Build 42 by default; the Build 41 layout is handled too.

## Contents

**[Chapter I. How Project Zomboid loads mods](#chapter-i-how-project-zomboid-loads-mods)**

- [Part I. The Build 42 folder layout](#part-i-the-build-42-folder-layout)
- [Part II. How the load order actually works](#part-ii-how-the-load-order-actually-works)
- [Part III. What the tool can and cannot tell you](#part-iii-what-the-tool-can-and-cannot-tell-you)

**[Chapter II. Getting started](#chapter-ii-getting-started)**

- [Part I. Install](#part-i-install)
- [Part II. A plain run](#part-ii-a-plain-run)
- [Part III. The interactive interface](#part-iii-the-interactive-interface)

**[Chapter III. Managing your mods](#chapter-iii-managing-your-mods)**

- [Part I. Choosing what you run](#part-i-choosing-what-you-run)
- [Part II. Actually disabling a mod](#part-ii-actually-disabling-a-mod)
- [Part III. Taking the load order into account](#part-iii-taking-the-load-order-into-account)
- [Part IV. Adding mods from the Workshop](#part-iv-adding-mods-from-the-workshop)
- [Part V. Mod images](#part-v-mod-images)
- [Part VI. Workshop links](#part-vi-workshop-links)

**[Chapter IV. Steam](#chapter-iv-steam)**

- [Part I. The Workshop lookup](#part-i-the-workshop-lookup)
- [Part II. Changing your subscriptions](#part-ii-changing-your-subscriptions)
- [Part III. Checking your subscriptions against the disk](#part-iii-checking-your-subscriptions-against-the-disk)

**[Chapter V. Configuration and output](#chapter-v-configuration-and-output)**

- [Part I. Settings](#part-i-settings)
- [Part II. Where the tool keeps your data](#part-ii-where-the-tool-keeps-your-data)
- [Part III. The log](#part-iii-the-log)
- [Part IV. Other useful options](#part-iv-other-useful-options)

**[Chapter VI. Building and extending](#chapter-vi-building-and-extending)**

- [Part I. Building a Windows executable](#part-i-building-a-windows-executable)
- [Part II. Code layout](#part-ii-code-layout)
- [Part III. Adding a rule](#part-iii-adding-a-rule)
- [Part IV. A note on the font](#part-iv-a-note-on-the-font)

## Chapter I. How Project Zomboid loads mods

### Part I. The Build 42 folder layout

This matters, because getting it wrong is the difference between a clean report
and a page of imaginary conflicts.

A Build 41 mod is flat:

```
mods/OldMod/mod.info
mods/OldMod/media/lua/client/...
```

A Build 42 mod ships one folder per game version, each with its own `mod.info`,
next to a shared `common/`:

```
mods/CleanUI/common/media/...      always applied, no mod.info
mods/CleanUI/42.12/mod.info + media/
mods/CleanUI/42.15/mod.info + media/
mods/CleanUI/42.19/mod.info + media/
```

The game loads `common/` plus exactly one version folder: the one matching its
own version, or the closest one below it. A client on 42.20 therefore loads
CleanUI's `42.19` branch.

pzmodmanager does the same. It treats that whole tree as one mod, picks the branch
the game would pick, and indexes only that branch. `--build 42` means "the newest
42.x branch each mod offers"; `--build 42.15` pins the selection to what a 42.15
client would load. The branch actually used is shown in the report inventory.

### Part II. How the load order actually works

Worth stating plainly, because every rule in this tool depends on it.

#### A. The last mod in the list wins

The engine reads the `Mods=` line from left to right and stacks each mod's media
folders in that order. **The last mod in the list wins.** When two mods ship the
same file, or define the same item, recipe or map tile, the one further right
replaces the one before it, silently, with no error anywhere.

#### B. The game does not sort dependencies for you

A mod that needs a framework has
to sit *after* that framework in the list. `require=` in mod.info declares the
relationship but does not reorder anything. That is exactly what the manager
fixes: its export sorts the selection so every mod comes after what it requires.

#### C. `Mods=` and `WorkshopItems=` do different jobs

They do different jobs. `WorkshopItems=` is the list of
Steam item ids a server downloads; its order means nothing. `Mods=` is the list
of mod ids the game loads, and its order is the one that matters.

The usual rule of thumb, in three tiers: frameworks and libraries first, content
mods next, patches and overrides last, since a patch only works if it lands on
top of what it patches.

#### D. Where this comes from

Sources for the above, since it is not documented by the developers:
[pzmod.dev's load order guide](https://pzmod.dev/guides/project-zomboid-mod-load-order/)
and the [Build 42 multiplayer mods guide](https://projectzomboid.wiki/multiplayer/mods/).
Both are community documentation rather than official, and they agree with each
other and with what the file stacking does on disk.

### Part III. What the tool can and cannot tell you

Project Zomboid has no notion of two mods being "compatible". The engine loads
mods in order and stacks their `media/` folders: when two mods ship the same
file, the one loaded last wins, silently. There is no compatible/incompatible
verdict to compute.

#### A. What it detects

Here is what pzmodmanager actually detects, most reliable first:

| Finding | Reliability | Severity |
|---|---|---|
| A dependency listed in `require=` is missing | certain | critical |
| Two folders declaring the same `id=` | certain | critical |
| An `incompatible=` field pointing at an installed mod | certain | critical |
| A dependency loaded after the mod that needs it | certain | high |
| A mod in the list that is not installed | certain | high |
| The same Lua file shipped by several mods | certain, verdict certain | high |
| A Workshop item that no longer exists | certain, needs the Steam lookup | high |
| An incompatibility stated in a Workshop description | read from prose, confirm it | high |
| The same script object (`item`, `vehicle`, `recipe`) redefined | certain, intent is yours to judge | medium |
| A mod with no folder for the build you play | certain | medium |
| The same texture, sound or model shipped twice | certain, cosmetic impact | low |

#### B. One cleaning rule, at the parser

`mod.info` is typed by hand and nothing checks it. A real machine has
`require=\damnlib` sitting next to a mod whose id is `damnlib`, and
`incompatible="TombBodyTex"` next to `TombBodyTex`. Every one of those has to be
seen through before anything is compared.

That cleaning used to happen at each comparison, and five places compare a mod
id. Four remembered and one did not, a different one each time, and every miss
was silent:

| Where | What it looked like |
|---|---|
| the scan's rules | an installed library reported missing |
| the manager panel | still missing, after the scan was fixed |
| the dependency closure | "missing from disk" in the footer |
| **the sort** | a library loaded after the mods needing it, while the panel said "order resolved" |
| **the incompatibility check** | two mods reported as fine that the game refuses to load together |

The last one is the worst kind of failure a checking tool can have: a false all
clear. On a real 245 mod list it hid 16 declared incompatibilities.

So the cleaning moved to `modinfo.clean_mod_id`, applied once when the file is
read. Comparisons now start from clean ids and a sixth comparison site inherits
that for free. What the author actually typed is kept alongside, in
`requires_raw` and `incompatible_raw`, and used only for reporting: the game
reads the same raw line and will still complain about it, so it is worth showing.

A test asserts the invariant directly, over a deliberately nasty `mod.info`: once
parsed, no id in `requires` or `incompatible` carries a stray character.

#### C. What it cannot see

What it does **not** see: purely logical incompatibilities, where two mods hook
the same function cleanly but with contradictory assumptions. No static analysis
catches those. The right source for them is the Lua errors in the game logs,
which name the offending file.

A script collision is often **deliberate**: a rebalance mod exists precisely to
overwrite another mod's values. The tool reports, it does not judge.

## Chapter II. Getting started

### Part I. Install

Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

Two dependencies: `rich` for the console output, `textual` for the interface.

### Part II. A plain run

```bash
python -m pzmodmanager
```

It looks for your Steam libraries and your `Zomboid` folder on its own, tells you
what it is doing as it goes, then writes an HTML report. The findings themselves
are not dumped in the terminal, they belong in the report.

```
pzmodmanager

  · Searching for the game folder...
  · Searching for the Steam library...
  · Scanning Workshop folder: C:\Program Files (x86)\Steam\steamapps\workshop\content\108600
  ·   107 mod(s) found there
  · Game folder found: C:\Users\you\Zomboid
  · 107 mod(s) discovered.
  · Indexing mod files... 107/107
  · 48213 file(s) indexed.
  · Load order found: C:\Users\you\Zomboid\Lua\saved_modlists.txt
  · Analysing overlaps...
  · Done in 12.4s: 23 finding(s).

Severity  Findings
critical         2
high             9
medium           7
low              5
info             0

Report : C:\...\pzmodmanager-report.html
Log    : C:\Users\you\AppData\Local\pzmodmanager\pzmodmanager.log
```

Add `--open` to have the report opened in your browser as soon as it is written.
Add `--quiet` to drop the progress lines. Add `--print-findings` if you really do
want everything in the terminal.

### Part III. The interactive interface

```bash
python -m pzmodmanager --tui
```

#### A. The main menu

Arrow keys to move, ENTER to select.

```
  Use arrow keys to move, ENTER to select
  Press 'Q' to quit

                    ...banner...
                 by superbagel9079

      first run                       every run after that

  +------------------+            +------------------+
  |     Results      | greyed     |   Last results   | <- inverse video
  |   Manage mods    | greyed     |   Manage mods    |
  |     Add mods     |            |     Add mods     |
  |      Scan        | <- video   |      Rescan      |
  |     Settings     |   inverse  |     Settings     |
  |      Quit        |            |      Quit        |
  +------------------+            +------------------+
```

The menu also has **Manage mods**, greyed out until a scan exists, and `M` from
the results screen jumps straight there.

On a first run the menu offers a greyed out **Results** and a plain **Scan**,
because there is nothing to show yet. Once a scan has run, the result is saved,
and every later launch offers **Last results** and **Rescan** instead, with the
date and size of the last scan in the footer.

**An entry is greyed when there is nothing behind it, not when a file is
missing.** A scan that found no mods is saved exactly like one that found a
hundred, so judging by the file alone offered **Last results** onto an empty
screen. **Results** needs a scan that saw something; **Manage mods** needs mods
to list. When a scan came back empty the footer says so and points at the folder
settings, rather than leaving you to open an empty screen and wonder.

- **Scan** / **Rescan** runs the scan and shows every step live, then opens the
  results.
- **Results** / **Last results** reopens the saved scan without redoing it.
- **Settings** edits everything the tool uses and saves it, so nothing has to be
  retyped on the command line next time.

#### B. The results screen

The findings are on the left, the detail on the right, and
the current line is in inverse video. Severity is read from the marker rather
than from a colour, `[!!!]` critical, `[!! ]` high, `[!  ]` medium, `[ . ]` low.
That is deliberate: it stays readable over SSH, on a black-and-white terminal, or
under a colour scheme that remaps the palette.

| Key | Effect |
|---|---|
| Arrows | move through the list |
| 1 to 5 | show only one severity |
| 0 | show everything again |
| R | open the HTML report |
| `h` | hide the minor problems, cycling: everything, hiding low, critical only |
| ESC | back to the menu |
| Q | quit |

The banner is 59 columns wide: below that terminal width it will be clipped. The
rest of the interface adapts.

## Chapter III. Managing your mods

### Part I. Choosing what you run

```bash
python -m pzmodmanager --manage
```

#### A. The manager screen

A list of every installed mod with a checkbox, a search box, and a panel that
revalidates the whole selection on every keystroke.

```
  ON   MOD                        ID
  [x]  ! Better Sorting           BetterSorting
  [x]  ! Duplicate Mod            DuplicateMod
  [ ]    Hardcore Zombies         HardcoreZombies   <- inverse video
  [x]  ! Inventory Tetris         InventoryTetris
  [x]    Quiet Mod                QuietMod
```

A `!` marks a selected mod involved in a problem. The side panel names the
problems and, for the highlighted mod, what it requires, what it conflicts with,
what would break if you dropped it, and its Workshop link:

```
  Quiet Mod
    id        QuietMod
    workshop  2392709985
    link      https://steamcommunity.com/sharedfiles/filedetails/?id=2392709985
              press 'w' to open it in Steam
```

| Key | Effect |
|---|---|
| `x` or SPACE | select or deselect the highlighted mod |
| / | search by name or id |
| a / n | select everything, or nothing |
| r | restore the list the scan found enabled |
| o | show the load order that will be exported, numbered |
| b | pin this mod to load before another one |
| v | list your pins, and remove any |
| g | write the order into a save, after confirmation |
| d | pull in every missing dependency |
| w | open this mod's Workshop page in Steam |
| e | export the selection |
| h | hide the minor problems: everything, hiding low, critical only |
| u | unsubscribe the deselected mods from Steam, after confirmation |
| ESC | back to the menu |

`r` was `o` until recently, and was labelled "from the load order". Both halves
were wrong. It never touched the order, only which mods are on; and what it puts
back is a snapshot taken by the scan, so a list you saved in the game afterwards
is not in it. It now names the file it read and the date it read it, and refuses
outright when the scan found no list at all, because in that case every mod is
recorded as enabled and restoring would tick all of them.

#### B. Selecting and deselecting

A large mod set produces a lot of `low` findings, most of them typos in other
people's `mod.info`, and a dozen of those bury the one critical that actually
stops the game loading. `h` cycles the problems panel through everything, hiding
low, and critical only. The `!` in the list follows it, since that marker means
"involved in a problem" and has to mean the problems you are looking at: with
every mod flagged by a low typo, two hundred exclamation marks say nothing at
all. It hides rows, never facts: the footer keeps counting every problem by
severity whatever the panel shows.

**Selecting a mod pulls in its dependencies automatically**, transitively, and
says which ones it added. **Deselecting** never removes anything else: it tells
you which selected mods still need what you just dropped, and leaves the choice
to you. Incompatibilities, dependency cycles and serious file collisions are
reported, never resolved behind your back.

#### C. Seeing the order before you export it

The list is alphabetical, which is right for finding a mod and wrong for
answering "what order will these load in". `o` swaps to the load order view: the
selected mods in the sequence that will be written out, numbered, with everything
unselected below them marked `.` because it has no place in an order it is not
part of.

```
  ON   MOD                            ID
  [x]    1  Core Library               CoreLib
  [x]    2  Weapon Framework           WeaponFw
  [x]    3  Brita's Weapon Pack        Brita
  [ ]    .  Hardcore Zombies           HardcoreZombies
```

This is the same computation the export runs, not a second one that might
disagree. That mattered: the panel's `order: resolved` line used to sort without
your existing order as a tie break while the export sorted with it, so the panel
could vouch for a sequence that was not the one written to the file. Both now go
through one function, and a test compares the rows on screen with the exported
list.

#### D. Load order instructions written in prose

`require=` is the only ordering a mod declares in a form a machine can read, and
the tool resolves it. A great deal of ordering is not declared there at all. It
is written on the Workshop page, in sentences:

```
  order     its Workshop page says where to put it:
            NEEDS TO BE LOADED AFTER ELLIE'S TATTOO PARLOR
```

Those lines are pulled out of the description at scan time and quoted twice: in
the side panel of the mod they belong to, and in a block of their own at the
bottom of the panel:

```
  LOAD ORDER NOTES (3)

    Not problems, and not counted as any. These pages say where
    to place the mod, in words no tool can turn into an order.

    ElyonLib
        Load it above those mods in the mod list.
        https://steamcommunity.com/sharedfiles/filedetails/?id=3384377738
```

**They are not problems and are not counted as problems.** The first version of
this put them in the problems panel at `medium` severity, which was a mistake
worth recording: three quoted sentences appeared as three new errors under a
heading that says PROBLEMS, complete with an exclamation mark in the list beside
mods that had nothing wrong with them. The problem total is for defects. A
quotation from a Workshop page is not one.

The detector is deliberately conservative. On a real set of 203 subscriptions it
finds 14 items. Bug report boilerplate ("include your mod list/load order"),
headings, bare mod names, "load order doesn't matter", and comment or changelog
lines (`# check correct mod load order...` sitting under a Known issues heading)
are all rejected.

**Nothing is reordered on the strength of a sentence.** A line of prose is not a
dependency: it may name a mod you do not have, it may be telling you what not to
do, and acting on it would mean moving a mod because a regular expression matched.
The tool quotes it and links the page. The placing is yours.

#### E. Pinning an order by hand

`require=` says a mod must be **present**, not that it must come **first**, and
almost everything else about ordering is prose on a Workshop page that no tool
can safely act on. A pin is you writing that ordering down once, in a form the
sort can use.

Highlight the mod that has to load first and press `b`. The panel holds it:

```
  PINNING    ElyonLib loads first
             highlight the mod it comes before, press 'b'
             'v' cancels
```

Highlight the other mod, press `b` again, and the pin is saved. From then on it
is treated exactly like a declared requirement when the order is worked out,
because that is what it is, written down by you instead of by the author.

`v` lists them and lets you take any back out. ESC there changes nothing; `s`
saves.

Three things worth knowing:

1. **A pin that would close a loop is refused at the moment you make it**, and
   both mods are named. Accepting it would produce an order nothing can satisfy,
   and the only symptom would be a cycle warning somewhere else entirely.
2. **A pin naming a mod you do not have, or have not selected, does nothing** and
   is not an error. Mods come and go; the file should not need curating every
   time one does. The panel shows two counts when they differ: how many pins you
   have, and how many are actually shaping this order.
3. **`(everything else)` is a valid side of a pin.** "Load this above all
   others" and "put this at the end" are things authors actually write, and
   writing them as one pair per mod would be two hundred lines that go stale the
   moment you add a mod. One pin covers it, resolved against the selection each
   time.
4. They live in `load-order-pins.json` next to your other data, and only
   **Reset everything** in the settings clears them. Clearing the last scan or
   the selection leaves them alone, since they are something you typed.

#### F. Reading the game's own log

Everything above is a prediction. The game keeps a record, and it is the only
thing that knows what actually happened. Every launch it writes `console.txt`
into its data folder (`~/Zomboid/console.txt`, with the previous sessions kept
under `Logs/`), and in it:

```
LOG  : Mod          f:0> loading ETO_B
LOG  : Mod          f:0> mod "ETO_B" overrides media/textures/vehicles/stepvan_1.png
```

The load order it really applied, and the winner of every file more than one mod
supplies. **Game log** on the main menu reads it and shows three things.

**Errors, grouped by shape.** A real session produced 6113 error lines, which is
the sort of number that makes people stop reading. They were seven distinct
problems: 5952 of them one missing skeleton bone name repeated over 26 bones,
149 missing vehicle templates over 124 names, and a handful of translation
format warnings. Quoted names and numbers are what varies, so the group counts
both the lines and the distinct subjects, which is what tells you whether a
problem is about mods you actually have.

**Files lost to a later mod.** The mod loaded last wins a file both supply, and
the loser's version is simply not used, with no error and nothing on screen.
This is the game's own verdict, not a guess from the order. A big number is not
automatically wrong: a texture optimiser is meant to be overwritten and a patch
is meant to overwrite. A mod losing most of its files to something unrelated is
the case worth opening.

**The order applied, against the order predicted.** If a mod the tool put first
was loaded fortieth, the order that was exported never reached the game, and
that is worth knowing before spending an evening reordering. When the log is
from a session with a different mod list, the screen says so instead of
comparing two things that have nothing to do with each other.

Nothing here writes anything. It reads one text file.

#### G. Applying the order to a save

The export writes files. **The game never reads them.** For a server that is
enough, because you paste the two ini lines yourself. For a single player game
it is not: Build 42 keeps the load order inside the save.

```
Zomboid/Saves/<mode>/<save name>/mods.txt

    VERSION = 1,

    mods
    {
        mod = ZombieBuddy,
        mod = AlicesMultiWearVanilla,
    }
```

Verified on a real machine: the sequence in that file is exactly the sequence
the game logs as it loads, mod for mod, 246 of them. So `g` in the manager
offers to write it, and it is the only thing in this tool that changes a file
belonging to the game.

Three guard rails, none of them optional:

1. **It reorders, it never adds or removes.** The set of mods in a save is part
   of that save: dropping one can break a world with items in the ground, and
   adding one mid-save is not a decision a tool should take for you. A write is
   refused unless the order holds exactly the mods the save already has, and the
   refusal names the difference. On a refusal the file is not opened for writing
   at all.
2. **A timestamped copy is taken first**, in the same folder, before a byte is
   written. `r` on that screen puts it back. The game has no undo for this.
3. **Cancel is the first and highlighted option** on the confirmation, which
   also lists which mods would move and to where. A stray ENTER does nothing.

**When the two lists differ, which is the normal case.** A save records the mods
that were active the last time it ran, so after switching a few variants off and
one on, the sets no longer match and the strict rule refuses. Stopping there
would be useless, so the screen offers to resequence the save's own mods
instead: the ones your order knows about take its sequence, and the ones it does
not know keep the exact index they already occupy, so nothing drifts around
them. The save keeps every mod it has and gains none, which is why this goes
through the same same-set check as any other write rather than around it.

Everything in the file that is not a mod line is carried through byte for byte,
including the version line, the maps block and the game's own indentation.
Rebuilding the file would mean guessing at a format that is not documented and
has changed between builds.

The full loop, once: work out the order, `g` to apply it, play, then **Game log**
to see the order the game really used and who won which file.

#### H. The export

The export produces a load order sorted so every mod comes after what it
requires, using your existing order to break ties so a working list is disturbed
as little as possible. You get the two lines a server ini needs:

```
Mods=Core;Lib;App;...
WorkshopItems=2392709985;3728775267;...
```

They are written next to the report, along with a plain mod list and a file of
Workshop links, and the selection is remembered for next time.

#### I. Without the interface

The same thing works without the interface, which is handy for scripting a
server:

```bash
python -m pzmodmanager --disable HardcoreZombies --enable Brita --print-order
python -m pzmodmanager --export-ini server-mods.ini
python -m pzmodmanager --print-links
python -m pzmodmanager --export-links workshop-pages.txt
```

Enables are applied before disables, so an explicit `--disable` always beats a
dependency that an `--enable` pulled in. The resulting gap is reported rather
than silently filled back, and exporting an unresolved selection prints a warning
rather than refusing.

#### J. On unsubscribing

There are two doors, and only one of them opens.

The Web API's `UnsubscribePublishedFile` is a publisher method on
`partner.steam-api.com`. A publisher key is scoped to its own publisher group's
app ids, so even a full Steamworks partner account gives you nothing for app
108600, which belongs to The Indie Stone. That door is shut, permanently.

The Steamworks SDK is the other door, and it is open. `ISteamUGC::UnsubscribeItem`
runs inside the Steam client, acts on whoever is logged in, and needs no key. See
the section below.

Deselecting a mod in the manager never touches your files or your subscriptions.
It only changes the list.

### Part II. Actually disabling a mod

Worth spelling out, because the selection on its own changes nothing.

#### A. The procedure

1. Open the manager: `python -m pzmodmanager --manage`.
2. Move to the mod and press SPACE. The checkbox empties and the problem panel
   updates. If other selected mods required it, the footer says which.
3. Press `e`. That writes three files next to the report:
   `pzmodmanager-server.ini.txt` with the two ini lines,
   `pzmodmanager-modlist.txt` with the plain list, and
   `pzmodmanager-workshop-links.txt` with the pages.
4. **Back up your server ini before touching it.** Copy it somewhere first: a bad
   `Mods=` line means the server refuses to start, or starts without the mods and
   your save loses everything those mods placed in the world.
5. Replace the `Mods=` and `WorkshopItems=` lines in the server ini with the
   generated ones, then restart the server.
6. Every player reconnecting has to have the same mods enabled, otherwise they
   get kicked or hit a black screen.

Nothing in step 1 to 3 touches your files or your Steam subscriptions. Deselecting
is entirely reversible: press SPACE again.

#### B. Deleting the files is a different question

That is a separate question, and usually not what you want.
Steam re-downloads a Workshop mod you are still subscribed to, so removing the
folder achieves nothing lasting. If you really want a mod gone from disk,
unsubscribe on the Workshop: press `w` on it in the manager, which opens the item
in the Steam client, and use the Unsubscribe button. The tool cannot do that for
you, as explained above.

### Part III. Taking the load order into account

Without an order the tool says "these three mods overlap". With it, it also says
"and this one wins". That changes how the report reads.

```bash
python -m pzmodmanager --order "%USERPROFILE%/Zomboid/Lua/saved_modlists.txt"
```

#### A. Accepted formats

Three formats are accepted and detected automatically: the client's
`saved_modlists.txt`, a server `.ini` containing `Mods=`, or a plain text file
with one mod id per line.

#### B. An honest caveat about `saved_modlists.txt`

Its format is not documented by
the developers and has already changed between builds. The tool assumes a list
name, then one id per line, with lists separated by a blank line. If the result
looks wrong, mods out of order, list truncated, do not fight it: export your
order to a plain text file and pass that with `--order`. That is the safe format.

When the file holds several lists the longest one is used, and the tool says so.
To pick another:

```bash
python -m pzmodmanager --order saved_modlists.txt --list-name "My solo run"
```

### Part IV. Adding mods from the Workshop

**Add mods** on the main menu, the opposite direction from the manager: the
manager only ever works with what is already on your disk, this finds what is
not.

#### A. Looking something up

Paste a Workshop link or an id into the box and press ENTER. Several at once is
fine, separated by spaces, commas or newlines, and a full page address works as
well as a bare number.

```
https://steamcommunity.com/sharedfiles/filedetails/?id=2392709985
2392709985 3728775267
```

Each result shows the title, the size, the last update, and its status against
your machine: **already installed**, **subscribed but not on disk yet**, **new**,
or **gone from the Workshop**. That last one matters, since subscribing to a
removed item downloads nothing.

The panel also shows the mod ids the description claims to install. Project
Zomboid authors write those by hand, by convention, because the Workshop has no
machine readable field for them. It is the only way to know what an item will
install before downloading it, and the screen says plainly that it is a hint
rather than a fact.

`x` or SPACE marks an item, `a` subscribes to everything marked, after one grouped
confirmation showing the full list and the total download size. The newest lookup
goes to the top of the list, and the cursor lands on it.

#### B. What can be known before downloading

A `!` on a row means read the panel first. It comes from two sources with very
different standing, and the panel keeps them apart rather than blurring them into
one list of "problems".

**The build tag is reliable.** Authors pick it from a fixed Steam list, so an
item tagged Build 41 while you target Build 42 is a stated fact, not a guess.
Build 42 changed the mod folder layout and much of the Lua API, so that one is
shown as a conflict. An item with no build tag at all is a warning: there is
simply no telling. Targeting Build 41 flips the test rather than hardcoding 42.

**Everything read from the description is a hint.** There is no machine readable
field for a mod id or a dependency, so authors type them into the description by
convention, in a dozen shapes. What the tool reads out of that is shown as
something to check on the page, never as a finding.

Checked against what you already have, from the last scan:

| What it spots | How sure |
|---|---|
| The item claims a mod id you already have installed | conflict, and it names the other Workshop item |
| A mod you have declares one of these ids incompatible | conflict, read from mod.info |
| The item is gone from the Workshop | conflict, subscribing brings nothing down |
| Its description names a dependency you do not have | warning, read from prose |
| It provides a dependency one of your mods is missing | good news, not a problem |

That last one is worth having: the scan tells you `WindSway` requires
`ZombieBuddy` and it is not installed, and this screen tells you when the item
you are looking at is the one that fills the gap.

The real dependency and conflict graph is only knowable once `mod.info` is on
disk, which is after the download. This is the part that can be known before, and
a Build 41 mod or a duplicate id is exactly what you want caught beforehand.

#### C. One item, several mods

A Workshop item and a mod are not the same thing, and the difference matters
more than it looks. "42.20 | Every Texture Optimized" installs two mods, `ETO_B`
and `ETO_P`, two variants meant to be used one at a time. Plenty of items do
this.

**Enabling is per mod. Subscribing is per item.** You subscribe to the item once
and then tick the mods you want in the manager, which is why the exported
`WorkshopItems=` line carries the id once while `Mods=` names only the variant
you chose.

**Unsubscribing is per item too, and Steam cannot remove part of one.** So the
manager will not unsubscribe from an item that still holds a mod you kept. If
you drop `ETO_P` but keep `ETO_B`, the confirmation screen says so plainly and
leaves the item alone:

```
  NOT UNSUBSCRIBED, because you kept part of them

  Workshop 3119788162
    you dropped  ETO_P
    you kept     ETO_B

  Steam cannot remove part of a Workshop item. Unsubscribing from
  one of these would delete the mods you kept as well, so they are
  left alone. Deselecting is enough: the game will not load them.
```

Deselecting is all you need: the game only loads what `Mods=` names. Dropping
every mod an item installs releases the whole item, and then it is offered.

The Add mods screen says when an item claims more than one mod id, because
finding out after the download that you have installed two conflicting variants
is a poor way to learn it.

#### D. Searching by name, and why it goes through Steam

Type a name instead of an id and press ENTER. The box decides which of the two
you meant: text with an id in it is looked up, text without one is searched for.
Steam's own Workshop search opens in your browser with that text. Copy the address of anything you like, paste it
back, and you get the full card.

That is one step more than searching inside the tool, and it is deliberate.
Looking an item up by id uses `GetPublishedFileDetails`, which is public and
needs nothing. Searching the whole Workshop by name is a different endpoint,
`IPublishedFileService/QueryFiles`, and it wants a Steam Web API key. The key is
free and anyone can create one, but it is a step this tool does not currently ask
anybody to take, and it could not be tested where this was written. Handing the
search to Steam costs nothing, uses the real thing rather than an approximation,
and works today.

#### D. What subscribing actually does

It tells Steam you want the item. Steam then downloads it in the background, in
its own time. **Nothing is on disk at the moment you confirm**, and a scan run
straight afterwards will not find the new mods. The tool says so rather than
pretending otherwise, and waits for you to press `r` when you judge the download
finished, which rescans and reopens the manager.

Subscribing is undone by unsubscribing. Nothing here is permanent, which is why
this screen carries a lighter warning than the unsubscribe one.

From the command line:

```bash
python -m pzmodmanager --add 2392709985
python -m pzmodmanager --add "https://steamcommunity.com/sharedfiles/filedetails/?id=2392709985"
```

Both look the item up, print what Steam knows about it, then ask you to type
`YES`. Nothing happens without that, or without `--yes` for a script.

### Part V. Mod images

The report shows each mod's picture in the inventory. By default it links the
Workshop preview, which keeps the file small and needs the Steam lookup to have
run. `--embed-images` inlines each mod's own `poster.png` instead, downscaled, so
the report works with no network at the cost of a bigger file. That option needs
Pillow.

The manager draws the local poster in the side panel. A terminal has no image
support to speak of, so the picture is built from half block characters: the
upper half block takes one colour for the top pixel and a background colour for
the bottom one, giving two pixels per cell. That works in any true colour
terminal, Windows Terminal included, rather than depending on Sixel or the Kitty
graphics protocol, which most terminals do not have. It is a low resolution
thumbnail, not a photograph, and that is the honest ceiling for a terminal.

Pillow is optional throughout. Without it the report still links the Workshop
previews, and the manager says so instead of showing a picture.

```bash
pip install Pillow
```

### Part VI. Workshop links

Every mod that came from the Workshop is linked to its page, in three places:

- in the HTML report, the mod names are clickable, in the inventory, in the
  most-involved table, and in the list of mods each finding names;
- in the manager, the full URL sits in the side panel, and `w` opens the item in
  the Steam client, where Unsubscribe lives;
- on export, `pzmodmanager-workshop-links.txt` lists the page of every selected
  mod in load order, which is what you hand to players who need to subscribe, or
  keep for rebuilding the same list on another machine.

Mods you installed by hand have no page, and are shown as plain text rather than
a dead link. `--print-links` prints the same list to the terminal.

## Chapter IV. Steam

### Part I. The Workshop lookup

By default the tool asks Steam about every Workshop mod it found, using the public
`GetPublishedFileDetails` endpoint. No API key, no login, one POST for every
hundred mods.

That buys three things the disk cannot tell you:

- the real Workshop title, so the report names mods the way the Workshop does;
- the last update date, shown in the inventory, which is how you spot a mod
  untouched since an older build;
- the description, where authors state incompatibilities in prose because there
  is no machine-readable field for it, and whether the item has been removed from
  the Workshop entirely.

Answers are cached for a day so repeated scans do not hammer the API. The lookup
is entirely best effort: no network, a proxy in the way or a Steam outage never
stops a scan, it just makes the report smaller. `--no-steam` turns it off,
`--refresh-steam` ignores the cache.

The incompatibility phrases are read from free text, so that finding says out
loud that you should confirm it on the Workshop page before acting on it.

### Part II. Changing your subscriptions

Optional, off unless you set it up, and the one part of this tool that reaches
outside and changes something you cannot undo from here.

#### A. What it needs

The Steamworks SDK redistributable, `steam_api64.dll`. It is
not shipped with this tool: download the SDK yourself and either drop the library
next to the tool or point at it.

Inside the SDK archive, the file you want is:

```
steamworks_sdk_165/sdk/redistributable_bin/win64/steam_api64.dll
```

Note `win64`, not the `steam_api.dll` sitting one level up: that one is 32 bit,
and 64 bit Python cannot load it.

#### B. Where to point it

**Either form works.** Give the dll itself, or the folder holding it. Both of
these are correct, and neither is better than the other:

```
C:\Users\leo\Documents\steamworks_sdk_165\sdk\redistributable_bin\win64\steam_api64.dll
C:\Users\leo\Documents\steamworks_sdk_165\sdk\redistributable_bin\win64
```

Given a folder, the tool looks inside it for `steam_api64.dll`, then
`steam_api.dll`. Given a file, it uses that file. Simplest of all, copy
`steam_api64.dll` next to the tool and set nothing: it is found on its own.

#### C. Changing it takes effect on the next scan

The subscription list is read once
per scan, not continuously, so after setting or correcting this path go back to
the menu and run **Scan**. The settings screen says so when you change the value.
Unsubscribing from the manager does not need a rescan first: it reads the library
at the moment you confirm.

#### D. Check it before you rely on it

```bash
python -m pzmodmanager --steam-check --steam-sdk C:/steamworks_sdk/redistributable_bin/win64
```

That reports what it found and changes nothing:

```
  library      C:/steamworks_sdk/redistributable_bin/win64/steam_api64.dll
  init symbol  SteamAPI_InitFlat
  UGC accessor SteamAPI_SteamUGC_v020
  app id       108600
  subscribed   139 item(s) visible
  result       usable
```

Run that first. If something does not work later, run it again: it is the whole
diagnosis in six lines.

#### E. How to use it

```bash
python -m pzmodmanager --unsubscribe SomeModId
python -m pzmodmanager --unsubscribe-unselected
```

Both print the full list of what they would unsubscribe, then wait for you to
type `UNSUBSCRIBE`. Nothing happens without that, or without `--yes` for a
script.

It works in the interface too. Pass `--steam-sdk` alongside `--manage` or
`--tui`, and `u` in the manager opens a full screen: the count as the headline,
every deselected mod listed rather than a truncated sample, and a menu that
starts on **Cancel**, so a stray Enter changes nothing. Confirming moves to a
second screen that shows each call as it goes and then the verified result, with
the subscription count before and after.

```bash
python -m pzmodmanager --manage --steam-sdk "C:/steamworks_sdk_165/sdk/redistributable_bin/win64"
```

#### F. Steam runs in a process of its own, and this is not decoration

The first version called the library straight from the interface's worker thread.
It froze the screen, and the reason is worth knowing because it is invisible in a
traceback. The Steam library prints from C to file descriptors 1 and 2, which
Python cannot intercept by reassigning `sys.stdout`, so the code redirected the
descriptors themselves while Steam ran. That is correct in a plain console. In a
full screen interface it is fatal: Textual draws the screen by writing to
descriptor 1, so for as long as the redirect was up, every frame went into the
temporary file instead of the terminal. Nothing was hung. The picture had simply
stopped arriving, which looks exactly like a freeze and lasts as long as Steam
takes to answer.

Every Steam call now happens in a child process, which fixes the whole family of
problem at once:

- the child owns its own descriptors, so Steam's printing goes to the log and
  never near the terminal the interface is drawing on;
- the child has a deadline and is killed if it passes, so a Steam client that is
  starting, updating, or waiting on something in its own window can no longer
  hang the tool. You get a message saying so and the scan carries on without the
  subscription check;
- `steam_appid.txt` is written in the child's own temporary folder, so nothing is
  left in yours;
- if the library falls over, and a foreign DLL loaded by ctypes certainly can, it
  takes the child with it and not your session.

Progress lines come back from the child as they happen, so a hundred item
unsubscribe still looks alive rather than frozen.

Results are verified by reading your subscription list back afterwards and
comparing, rather than trusting the asynchronous callback, so the tool tells you
which ones actually went.

#### G. Three things to know before using it

The process has to identify itself to Steam as an app, which here means claiming
app id 108600. Your tool tells Steam it is Project Zomboid. Every third party
Workshop manager does this, and it is a grey area in Steam's terms rather than a
documented, blessed route. The app id is set through an environment variable, and
the `steam_appid.txt` fallback is deleted again afterwards.

Unsubscribing removes the local files once Steam next shuts down. On the machine
that also feeds your server, the mod is gone for you too, and a save that relied
on it loses what it placed in the world.

#### H. How far this was actually verified

Against SDK 1.65 specifically: loading the
library, resolving every symbol used, finding the UGC accessor, and calling init
and reading back Steam's own error. Those all work. The signatures for
`GetNumSubscribedItems`, `GetSubscribedItems`, `SubscribeItem` and
`UnsubscribeItem` were taken from `steam_api_flat.h` in that SDK rather than
guessed.

Two things that release taught the code. The library exports `SteamAPI_InitFlat`
but not `SteamAPI_Init`, which is an inline helper in the C++ header rather than
a symbol, so anything looking for `SteamAPI_Init` alone finds nothing. And the
accessor is `SteamAPI_SteamUGC_v021`, a name that moves with every SDK, which is
why the code probes a range rather than hardcoding one.

What remains untested anywhere is the subscribe and unsubscribe calls themselves,
because those need a running Steam client and there was none. If something goes
wrong, `--steam-check` and the log say where.

The child process arrangement, by contrast, was tested properly, against a stand
in library built to behave like the real one: reading a subscription list from a
folder path and from a file path, unsubscribing with live progress, confirming
that nothing the library prints from C reaches the parent's own output, and
confirming that a library which never returns is killed at the deadline and
reported rather than hanging the tool. That last one is the freeze, reproduced
and then fixed.

### Part III. Checking your subscriptions against the disk

Once the Steam SDK folder is set, every scan also reads your subscription list
and compares it with what is on disk. That catches two things nothing else will.

#### A. Installed but no longer subscribed

Steam does not delete a mod's files when
you unsubscribe: it waits until it next shuts down. Until then the game loads
them exactly as before. This is why unsubscribing looks like it did nothing, and
why a server can quietly keep running a mod that nobody can install any more.

#### B. Subscribed but not installed

Steam lists the item for your account but
nothing arrived in the Workshop folder, usually a download that has not run or
has failed. A server listing it in `WorkshopItems` will stall.

Neither check says anything when the SDK is not configured or Steam is closed.
An unknown subscription list is not the same as an empty one.

This is also why unsubscribing from the manager **triggers a rescan**: the files
are still there, so the mod list does not shrink, but it comes back flagged as
still loading, which is the thing worth knowing before you start the game.

## Chapter V. Configuration and output

### Part I. Settings

Everything the tool uses lives on one screen, and is saved between runs:

| Setting | What it does |
|---|---|
| Data folder | where the saved scan, selection, cache and log are kept |
| Steam SDK | `steam_api64.dll` itself, or the folder holding it: both work |
| Target build | `42` for the newest 42.x branch, `42.15` to pin a client |
| Auto-detect locations | probe the usual Steam and Zomboid folders |
| Extra scan folders | additional folders, one per line |
| Load order file | a server ini, a saved list, or a plain text list |
| Steam Workshop lookup | fetch titles, dates and descriptions |
| Parse item scripts | read `media/scripts`, slower on a big mod set |
| Only analyse enabled mods | ignore mods absent from the load order |
| Report file | where the HTML report goes |

ENTER changes a value, `D` clears one, and changes are written straight away.
What was detected on this machine is shown underneath, which is usually the
thing you want to copy into a field above.

#### A. Saved now, read at the next scan

Every setting in that table describes how
mods are read from disk, and mods are read during a scan. So changing one saves
immediately but changes nothing you are looking at: go back to the menu and run
**Scan**. The screen says so on the line under the table each time you change a
value, because this is the sort of thing that is obvious once and confusing
every other time.

#### B. Starting over

Four actions at the bottom of the same screen clear what the tool remembers about
itself: the last scan, the saved selection, the Workshop cache and its preview
images, or all of that plus every setting back to its default.

ENTER arms one, ENTER again carries it out, and moving to another row disarms it.
Nothing outside the tool is touched by any of them: no mod, no save, no server
file. Clearing the last scan puts the menu back to offering **Scan** rather than
**Last results**, which is also the quickest way to see the first-run interface
again.

#### C. Command line arguments still win

A saved setting is a default, not a lock.
Passing `--build 42.19` runs with that and leaves the saved value alone. The tool
works out which options you actually typed rather than treating an argparse
default as a choice.

### Part II. Where the tool keeps your data

Five files, none of them in the folder the tool runs from by default:

| File | What it is |
|---|---|
| `last-scan.json` | the saved scan, reopened by **Last results** |
| `selection.json` | the mods you ticked in the manager |
| `settings.json` | the settings screen |
| `workshop-cache.json` | Workshop answers, kept for a day |
| `pzmodmanager.log` | the log |

| System | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\pzmodmanager` |
| macOS | `~/Library/Application Support/pzmodmanager` |
| Linux | `~/.local/state/pzmodmanager` |

They live outside the project on purpose: they are yours, not the program's, and
they should survive cloning the repository somewhere else, deleting `dist/`, or
rebuilding. That is why a fresh clone still opens on **Last results** rather than
on the first-run menu.

#### A. Moving them

Set **Data folder** on the settings screen, or pass `--data-dir`, and the saved
scan, the selection, the Workshop cache and the log all move there. Point it at
the project folder if you would rather keep everything in one place.

```bash
python -m pzmodmanager --data-dir .
```

The `.gitignore` already covers all four, so keeping them next to the code does
not put your scan or your mod selection in the repository.

**It takes effect on the next launch, not the next scan.** The log file is opened
before anything else happens, so the folder is settled once at startup. The
settings screen says so rather than promising a rescan will do it.

#### B. Except settings.json, which cannot move

It stays in the per-user location whatever the Data folder says, and that is not
an oversight. `settings.json` is the file that says where everything else goes.
If a setting could move it, the tool would have to read the file to find out
where the file is. Something has to be findable without configuration, and that
is the one.

### Part III. The log

Every run writes a log: the paths it probed, the mods it found, the rules it ran,
and every error it swallowed to keep going. When a scan returns something
surprising, the log is where the answer is.

By default it goes next to your user data, not into the current folder:

| System | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\pzmodmanager\pzmodmanager.log` |
| macOS | `~/Library/Logs/pzmodmanager/pzmodmanager.log` |
| Linux | `~/.local/state/pzmodmanager/pzmodmanager.log` |

`--log FILE` puts it somewhere else, `--log-level debug` makes it much more
detailed. Nothing from the log ever reaches the terminal, so it never fights with
the progress display.

### Part IV. Other useful options

| Option | Effect |
|---|---|
| `--path FOLDER` | scan an extra folder (repeatable) |
| `--no-auto` | do not probe the usual Steam and Zomboid locations |
| `--build 41` | read a mod's Build 41 branch rather than its 42 one |
| `--only-enabled` | ignore mods absent from the load order |
| `--min-severity high` | keep only the serious findings |
| `--html FILE` | choose where the report goes |
| `--no-html` | skip the report |
| `--json FILE` | machine-readable export, to diff two states of the mod set |
| `--no-scripts` | skip `media/scripts` parsing (faster) |
| `--fail-on critical` | exit code 1 if a critical finding remains |
| `--no-steam` | never touch the network |
| `--refresh-steam` | ignore the Workshop cache |
| `--font FILE` | embed a TTF in the report for headings and figures |
| `--embed-images` | inline mod posters in the report instead of linking previews |
| `--data-dir FOLDER` | where the scan, selection, cache and log are kept |
| `--state FILE` | where the last scan is saved |
| `--manage` | open the mod manager |
| `--enable ID` / `--disable ID` | change the saved selection without the interface |
| `--export-ini FILE` | write the server ini lines |
| `--print-order` | print the resolved load order |
| `--print-links` | print the Workshop page of every selected mod |
| `--export-links FILE` | write those links to a file |
| `--selection FILE` | where the selection is stored |
| `--steam-check` | report what the Steam bridge can do, change nothing |
| `--steam-sdk PATH` | the Steamworks redistributable, or its folder |
| `--add ID_OR_URL` | subscribe to a Workshop item, after confirmation |
| `--unsubscribe ID` | unsubscribe from a mod, after confirmation |
| `--unsubscribe-unselected` | unsubscribe from everything not in the selection |
| `--yes` | skip the typed confirmation |

`--help` lists everything.

## Chapter VI. Building and extending

### Part I. Building a Windows executable

```bash
pip install pyinstaller
pyinstaller pzmodmanager.spec
```

The executable lands in `dist/pzmodmanager.exe` and takes the same options:

```
pzmodmanager.exe --tui
```

**Build from the spec file, not from `pzmodmanager/__main__.py`.** That file starts
with a relative import, which is right for `python -m pzmodmanager` but fails the
moment PyInstaller runs it as a top level script:

```
ImportError: attempted relative import with no known parent package
```

The spec builds `run-pzmodmanager.py` instead, which uses absolute imports, and it
collects Textual and Rich in full. Both load data files at runtime, stylesheets
and terminal tables, that PyInstaller does not find by following imports alone.
Without that the executable starts and then dies as soon as the interface opens.

If you would rather type it out than use the spec:

```bash
pyinstaller --onefile --name pzmodmanager --collect-all textual --collect-all rich --collect-submodules pzmodmanager run-pzmodmanager.py
```

This build was verified: the resulting binary runs a full scan, writes the
report, opens the interface and embeds posters. It was built on Linux, so the
Windows executable itself is still unverified, but the packaging problem the
spec solves is not platform specific.

Expect roughly 40 MB, and expect an antivirus to look twice at any PyInstaller
executable.

### Part II. Code layout

```
pzmodmanager/
  models.py      Mod and Finding, the severity scale
  builds.py      Build 42 version folders and which branch the game loads
  discovery.py   finds mods (multi-library Steam Workshop, Zomboid/mods)
  modinfo.py     mod.info parser, tolerant about encodings
  assets.py      per-mod media/ file index, B41 and B42 branch handling
  scripts.py     brace parser for media/scripts/*.txt
  loadorder.py   reads the load order
  analyzers.py   the detection rules
  pipeline.py    the scan sequence shared by the CLI and the interface
  logs.py        log file setup
  settings.py    what the tool remembers between runs
  selection.py   dependency closure, validation, load order, ini export
  steam.py       Workshop lookups, their cache, and reading pasted ids
  store.py       saves the last scan so a later launch can reopen it
  fonts.py       embeds a display font in the report
  posters.py     finds mod artwork and draws it as terminal half blocks
  steamsdk.py    ctypes bridge to ISteamUGC, only ever run in the child
  steambridge.py starts that child, tails its progress, enforces a deadline
  steamworker.py the child itself: one request in, one answer out
  report.py      HTML, JSON and console output
  tui.py         the interactive interface
  manager_screen.py     the mod manager screen
  settings_screen.py    the editable settings screen
  browse_screen.py      finding Workshop items and subscribing to them
  unsubscribe_screen.py the confirm and run screens for unsubscribing
  cli.py         command line
tests/
  make_fixture.py      builds a synthetic mod tree
  test_pzmodmanager.py   checks every rule and the absence of false positives
```

Run the tests:

```bash
python tests/test_pzmodmanager.py
```

### Part III. Adding a rule

A rule is a function taking an `AnalysisContext` and returning `Finding` objects.
Write it in `analyzers.py` and add it to the `ALL_RULES` list, the console, HTML,
JSON and interactive outputs all pick it up with no further change.

Two natural next steps: detecting cell overlap between map mods (reading the
`<x>_<y>.lotheader` files), and reading the game logs to tie a Lua error back to
the mod that caused it.

### Part IV. A note on the font

The report can use **Pixter Granular** by Matt Grey, or any other TTF or OTF you
point at with `--font`. Drop `pixter-granular.ttf` next to the tool and it is
picked up on its own. The font travels inside the report as a data URL, so the
file stays self-contained.

It is applied to the headings, the severity tags and the big figures, not to the
body text. Pixter Granular is a display face and it is a proportional one: lovely
on a title, hard work across nine hundred lines of findings. The report exists to
be read.

It cannot be used in the interactive interface at all. A terminal draws
everything with the font the terminal itself is configured with, and no program
running inside it can change that. Even if you set it as your terminal font, a
proportional face would break the ASCII banner and every aligned column, because
that art depends on every character being exactly one cell wide.

Pixter Granular is licensed free for personal use, so it is not bundled here. You
supply the file.
