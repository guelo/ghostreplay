# Ghost Replay User Acquisition & Learning Plan

**Revised 90-day marketing strategy**

> **Primary objective:** Acquire players who complete the Ghost loop—not merely visitors, anonymous accounts, or game starts.

**Prepared for:** Ghost Replay  
**Version:** 1.1  
**Date:** August 11, 2026

## How to use this plan

This document combines acquisition, activation, measurement, and monetization learning into one operating plan. The sequence matters: first make the distinctive Ghost experience fast and observable; then recruit a small cohort; then test public channels; only after that should Ghost Replay invest in scale.

### Contents

| **Section**                             | **Purpose**                                                                    |
|-----------------------------------------|--------------------------------------------------------------------------------|
| **Executive summary**                   | The strategy, audience, north stars, and immediate priorities.                 |
| **1. Growth objective and definitions** | What counts as a visitor, target owner, activated player, and retained player. |
| **2. Audience and positioning**         | Who to target first and how to describe the product.                           |
| **3. Landing page and activation**      | Messaging, first-ghost experience, discoverability, and product bridges.       |
| **4. Revised 90-day rollout**           | A sequenced plan with internally consistent exit criteria.                     |
| **5. Acquisition channels**             | Founder-led recruiting, coaches, creators, Reddit, Show HN, and search.        |
| **6. Analytics and experiment design**  | A corrected funnel mapped to the current event taxonomy.                       |
| **7. Qualitative research**             | Observed sessions, feedback questions, and privacy-conscious learning.         |
| **8. Sharing and re-engagement**        | Product loops that can make acquisition compound.                              |
| **9. Monetization learning**            | How behavior can inform concrete willingness-to-pay tests.                     |
| **10. First seven days**                | A focused implementation and recruitment checklist.                            |
| **Appendices**                          | Copy, outreach templates, launch checklists, and sources.                      |

### Revision audit

| **Feedback addressed**       | **Resolution in this revision**                                                                                                                                                                                                                  |
|------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Analytics taxonomy**       | Preserve existing `blunder_recorded`, `srs_review_recorded`, `game_started`, `game_ended`, and `opponent_move_served` events. Add only exact Ghost arrival plus return context; do not create parallel capture or grading event names. |
| **Week 1 sequence**          | Recruit ten players and observe five during Week 1. Week 1 exits on evidence from those five. Weeks 2-4 recruit the remaining fifteen and complete ten total observed sessions.                                                                  |
| **Channel-policy citations** | Use canonical rule pages with no tracking parameters. The rules summarized here were checked on 11 August 2026, but every community rule must be rechecked within 24 hours of posting.                                                           |

> **Operating principle**
>
> Do not judge a channel by clicks. Judge it by the number of people who reach a personal Ghost position, make a new decision, receive a grade, and later return.

## Executive summary

Keeping Ghost Replay free while the product learns from early users is a sound strategy. The important correction is to optimize for activated players rather than registrations or traffic. New visitors receive anonymous accounts, so account creation is not a meaningful growth signal by itself. The valuable moment is when a player encounters a position connected to a prior mistake, chooses again, and receives a pass/fail review.

Ghost Replay should own one memorable idea: an opponent that remembers your mistakes. The full product loop is play, analyze, capture a target, reach that position again in a later game, and review the new decision. <sup>[1]</sup>

### Strategic choices

| **Decision**             | **Recommendation**                                                                                                                              |
|--------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| **Primary audience**     | Active online rapid players, approximately 800-1600 rating, who play recurring openings and keep repeating mistakes they have already reviewed. |
| **Primary promise**      | Stop making the same chess mistake twice.                                                                                                       |
| **Marketing north star** | Activated players by acquisition source.                                                                                                        |
| **Product north star**   | Weekly unique players completing at least one graded Ghost review in a natural later-game context.                                              |
| **First 30 days**        | Recruit and learn from a Founding 25 cohort; reduce time to first understandable Ghost experience.                                              |
| **Next 60 days**         | Test a small number of public channels, identify one repeatable source, and add a shareable result loop.                                        |
| **Monetization posture** | Keep the core Ghost loop free initially; test paid convenience, deeper analysis, or coach workflows only after repeated use reveals demand.     |

### The 90-day sequence

| **Phase** | **Verb**           | **Outcome**                                                                                                       |
|-----------|--------------------|-------------------------------------------------------------------------------------------------------------------|
| **1**     | Clarify            | Make the promise precise, show the mechanism in under a minute, and guarantee an understandable first experience. |
| **2**     | Observe            | Recruit ten players in Week 1, watch five onboard, and fix the largest activation failure.                        |
| **3**     | Validate           | Build the Founding 25 cohort and test whether activated players voluntarily return.                               |
| **4**     | Distribute         | Test founder-led, coach, creator, r/chess, Show HN, and search channels one at a time.                            |
| **5**     | Compound           | Add sharing and re-engagement only after the core experience predicts retention.                                  |
| **6**     | Monetize carefully | Test concrete paid propositions with repeat users, not speculative surveys of first-time visitors.                |

## 1. Growth objective and measurement definitions

The goal of the next 90 days is not maximum reach. It is to prove that a specific player segment repeatedly values the Ghost loop and to discover at least one acquisition source that produces those players at a repeatable rate.

### Define the player states before building dashboards

| **State**                   | **Canonical measurement**                                                                                    | **Why it matters**                                                                             |
|-----------------------------|--------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| **Visitor**                 | `$pageview` on the public landing experience.                                                             | Useful for denominator and source attribution; not a success state.                            |
| **Game starter**            | `game_started`.                                                                                            | Shows intent, but not yet product understanding.                                               |
| **Target owner**            | `blunder_recorded` or `blunder_added_manual`.                                                            | A personal learning object exists.                                                             |
| **Bridge-activated player** | `ghost_position_reached` followed by `srs_review_recorded` in a seeded-demo or immediate-replay context. | The player understands the mechanism, but has not yet experienced the full later-game promise. |
| **Core-activated player**   | `ghost_position_reached` followed by `srs_review_recorded` in a natural later-game context.              | The distinctive Ghost Replay value has occurred.                                               |
| **Returned player**         | A later `game_started` with return properties showing a subsequent game or product session.                | Use the existing game-start event instead of inventing a duplicate second-session event.       |
| **Retained player**         | A returned player in the chosen window, segmented by activation state.                                       | Compare 7-day and 28-day return rates for activated and non-activated cohorts.                 |

### North stars and guardrails

- **Marketing north star:** activated players per acquisition source and campaign.

- **Product north star:** weekly unique players completing at least one graded natural Ghost review.

- **Activation guardrail:** median time from first game start to first graded review.

- **Quality guardrail:** first-game completion, error rate, and review-grade integrity.

- **Privacy guardrail:** collect product events needed for learning, keep broad session recording off, and explain gameplay-data use clearly.

> **Do not collapse bridge and core activation**
>
> A seeded demonstration or immediate replay is valuable because it teaches the concept quickly. It should be measured separately from a Ghost that returns organically inside a later game, which is the product's strongest promise.

## 2. Audience and positioning

### Initial audience

Begin with one coherent segment rather than marketing to every chess player: active online rapid players rated roughly 800-1600 who play at least three games per week, revisit the same opening families, sometimes review their games, and still recognize familiar mistakes only after repeating them.

- They have enough recurring play for personal patterns to emerge.

- They care about improvement but may dislike grinding generic puzzle sets.

- They can describe the frustration in ordinary language: “I already knew that, but I did it again.”

- They are numerous enough to recruit manually through clubs, Discords, coaches, and personal networks.

### Positioning statement

> **For active chess improvers who keep repeating mistakes they already reviewed, Ghost Replay is a free chess trainer whose opponent remembers those mistakes and brings them back inside later games. Unlike ordinary analysis or generic puzzles, it gives you another chance under game conditions.**

### Message hierarchy

| **Priority**                 | **Message**                                                                                                      |
|------------------------------|------------------------------------------------------------------------------------------------------------------|
| **1. Outcome**               | Stop making the same mistake twice.                                                                              |
| **2. Mechanism**             | The opponent remembers a prior mistake and can steer a later game toward it.                                     |
| **3. Experience**            | Choose again under game pressure, then receive a pass/fail review.                                               |
| **4. Friction reducer**      | Free, no signup, runs in the browser.                                                                            |
| **5. Technical credibility** | Stockfish, Maia-style opponent selection, spaced review, and opening analysis - after the benefit is understood. |

> **Positioning boundary**
>
> Do not lead with “AI-powered,” centipawn loss, model names, or opening drills. Opening drills can support retention, but the returning-mistake mechanic is the reason Ghost Replay is memorable.

## 3. Landing page, discoverability, and activation

### Make Ghost Replay the hero product

The current homepage presents Ghost Replay and Opening Drills as two parallel ways to train. That dilutes the distinctive story. Put the returning-mistake experience first and move Opening Drills below it as a secondary benefit. The current “Every costly move becomes a personal case file” wording is also broader than the documented automatic capture behavior, which is limited by evaluation loss, move range, and session frequency. <sup>[1, 2]</sup>

#### Recommended hero

| **Element**           | **Copy**                                                                                                                                               |
|-----------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Headline**          | Stop making the same chess mistake twice.                                                                                                              |
| **Subheadline**       | Ghost Replay remembers positions you mishandled and quietly steers later games back toward them - so you practice the better move under game pressure. |
| **Primary CTA**       | Face your first ghost                                                                                                                                  |
| **Secondary CTA**     | Watch the 45-second demo                                                                                                                               |
| **Proof line**        | Free \| No signup \| Runs in your browser                                                                                                              |
| **Secondary feature** | Also included: opening practice based on the repertoire you actually play.                                                                             |

### Show the mechanism in 45 seconds

| **Timing**    | **On-screen story**                                   |
|---------------|-------------------------------------------------------|
| **0-8 sec**   | A player makes a costly move. “Ghost captured.”       |
| **8-18 sec**  | Time transition: “Two games later...”                 |
| **18-30 sec** | The opponent's moves lead toward the stored position. |
| **30-40 sec** | The player chooses the better move.                   |
| **40-48 sec** | “Ghost defeated” plus streak or review result.        |

### Reduce time to an understandable first Ghost

| **Priority** | **Improvement**         | **Purpose**                                                                                                                   |
|--------------|-------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| **P0**       | Seeded sample Ghost     | Offer a two-minute sample in which the target position is guaranteed. This teaches the concept before asking for repeat play. |
| **P0**       | Immediate-replay bridge | After a qualifying mistake, offer “Face this position now, or let it return naturally later.” Track it as bridge activation.  |
| **P0**       | Visible state           | Show ghosts captured, due, opening family, and the next action. Never leave the player unsure whether capture worked.         |
| **P1**       | Paste a PGN             | Test a low-friction path: paste one Chess.com or Lichess game, choose one mistake, and face it immediately.                   |
| **P1**       | Claim after value       | Prompt “Keep your ghosts across devices” only after capture or review, not before the first game.                             |
| **P2**       | Automatic import        | Build only after manual paste or interviews show recurring demand.                                                            |

### Fix the public presentation before sending traffic

The public HTML currently has a generic title and default Vite favicon, with no useful description or social sharing metadata. The repository README also still describes a client scaffold and future Milestone 1 work, which understates the current product. <sup>[3, 4]</sup>

- Use a memorable custom domain and branded favicon.

- Add a descriptive title, meta description, canonical URL, and Open Graph/social card.

- Add `robots.txt`, a sitemap, Google Search Console, and Bing Webmaster Tools.

- Statically render or server-render public marketing, FAQ, and article pages; leave the game app as a client application.

- Rewrite the README opening with the product promise, live link, demo GIF, screenshots, differentiation, architecture, setup, and roadmap.

> **Suggested search presentation**
>
> Title: Ghost Replay Chess Trainer - Stop Repeating Your Mistakes  
> Description: A free chess trainer that saves mistakes from your games and brings the positions back later, so the better move sticks. Play in your browser with no signup.

## 4. Revised 90-day rollout

The milestones below are operating targets, not external benchmarks. They are designed to create a decision point at the end of each phase. The Week 1 sequence now includes recruitment and uses only the five observed sessions as its exit evidence.

| **Period**     | **Goal**               | **Actions**                                                                                                                                                                                   | **Exit condition**                                                                                                                                                                                   |
|----------------|------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Week 1**     | Clarify and observe    | Rewrite hero and capture language; create demo; add exact-arrival and return measurement; add a guaranteed bridge experience; recruit 10; observe 5; build the first funnel dashboard.        | 5 observed sessions completed; at least 4 of 5 can explain the mechanism; at least 3 of 5 complete a graded bridge review within 20 minutes; the largest activation blocker is named and measurable. |
| **Weeks 2-4**  | Founding 25 cohort     | Recruit the remaining 15 players; observe 5 more; ask each participant to complete three sessions in seven days; ship one activation improvement per week; collect three permissioned quotes. | At least 15 of 25 create a personal target; at least 10 complete a graded Ghost review; at least 5 return in a later week; ten total observed sessions are complete.                                 |
| **Weeks 5-8**  | Test public sources    | Publish six short demonstrations; make one eligible and rules-compliant r/chess post; launch on Show HN; contact 20 coach/creator prospects; publish two interactive search articles.         | At least one source produces 10 activated players. Sources are ranked by core activation, retention, and qualitative fit - not clicks.                                                               |
| **Weeks 9-12** | Make growth repeatable | Double down on the best one or two sources; add a shareable Ghost result; test one re-engagement message; publish more of the best-performing evergreen format.                               | At least 20 activated players per week for three consecutive weeks; the leading source repeats; activated-player retention is stable or improving.                                                   |

### Weekly operating cadence

| **Day**               | **Operating rhythm**                                                                       |
|-----------------------|--------------------------------------------------------------------------------------------|
| **Monday**            | Review funnel and retention by source; choose one activation hypothesis.                   |
| **Tuesday-Wednesday** | Implement or test the smallest product/messaging change.                                   |
| **Thursday**          | Run observed sessions or interviews; document exact confusion and language.                |
| **Friday**            | Review evidence; decide keep, change, or stop; prepare the next recruitment/content batch. |

> **Go / no-go discipline**
>
> Do not begin the broad public-channel phase merely because four weeks elapsed. Begin it when the first experience is understandable, exact arrival is measurable, and at least a small set of activated players voluntarily returns.

## 5. Acquisition channels, in priority order

### 1. Founder-led recruitment

This is the best source for the first 25 players because the objective is detailed learning, not scale. Recruit through personal contacts, local chess clubs, online club organizers, chess Discords with administrator approval, coaches, and players who already describe the repeated-mistake problem.

> **I’m testing a free chess trainer for players who keep recognizing a mistake only after they make it again. Instead of only showing analysis or an isolated puzzle, the opponent can bring the position back inside a later game. I’m looking for 25 rapid players who will play three sessions this week and tell me what is confusing. There is no payment, upsell, or required signup.**

- Offer a Founding Player badge, early feature access, and visible influence on the roadmap.

- Ask for a concrete commitment: three sessions in seven days plus a ten-minute conversation.

- Schedule observed sessions while recruiting; do not wait for unsolicited feedback.

### 2. Chess coaches

Contact 10-20 independent coaches and ask each to try Ghost Replay with one student who repeats an opening or early-middlegame mistake. The near-term goal is learning whether coaches value target assignment, student progress, game import, due-review visibility, and weekly summaries. Repeated demand would make a coach dashboard a credible paid product.

### 3. Small chess creators

Offer to turn one of a creator’s instructive losses into a compact “mistake, return, correction” demonstration. The clip should teach a chess idea even when nobody clicks. Repurpose the same core asset for vertical video and one text-led platform rather than creating four separate content operations.

> **Repeatable content format**
>
> The tempting move  
> Why it failed  
> The Ghost returns  
> The corrected decision  
> Try the position yourself

### 4. Reddit - conditional, not automatic

As of 11 August 2026, r/chess permits limited self-promotion only for accounts that participate broadly, using Reddit’s 10% guideline and moderator contact when uncertain. r/chessbeginners explicitly treats advertising a chess website you made as prohibited self-promotion. r/Chesscom asks members not to use the community to promote other platforms and to keep self-promotion minimal. <sup>[9, 10, 11]</sup>

| **Community**        | **Recommendation**             | **Action**                                                                                                                                                                              |
|----------------------|--------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **r/chess**          | Conditional launch option      | Confirm the account has a genuine contribution history; re-read rules; contact moderators when uncertain; disclose authorship; embed useful content; ask one focused feedback question. |
| **r/chessbeginners** | Do not use as a launch channel | Current rules explicitly include advertising a chess website you made in prohibited self-promotion.                                                                                     |
| **r/Chesscom**       | Do not use as a launch channel | Current rules discourage promotion of other platforms and ask that self-promotion remain minimal.                                                                                       |

> **Mandatory posting checkpoint**
>
> Re-open the actual subreddit rules within 24 hours of posting. Rules and moderator practice can change. Do not rely on this document as permanent permission, and do not append tracking parameters to rule citations.

### 5. Show HN

Ghost Replay fits Show HN because it is personally built, immediately tryable, technically interesting, and does not require signup. Keep the submission factual and personal, avoid marketing language, do not coordinate votes or comments, and make the demo link work before posting. <sup>[12, 13]</sup>

- Working title concept: “Show HN: Ghost Replay - a chess opponent that steers games toward your past mistakes.”

- Tell the technical story: browser engine, human-like opponent selection, target steering, anonymous-to-claimed account flow, and architecture.

- Write the final submission in your own voice. Use this plan as an outline rather than pasting generated launch copy.

### 6. Search-oriented content

- Why do I keep making the same mistakes in chess?

- How to review a chess game so the lesson actually sticks

- Spaced repetition for positions from your own games

- How to practice a mistake from a real chess game

- Why engine analysis does not automatically prevent repeated blunders

- A better way to learn from opening mistakes

Each article should contain one real position, a playable move choice, a concise explanation, a demonstration of the Ghost mechanism, and a CTA to create or import a personal target. Public pages should be statically or server rendered; Google recommends server-side or pre-rendering for speed and broader crawler compatibility, and descriptive titles and meta descriptions improve search presentation. <sup>[14, 15]</sup>

### 7. Product Hunt and paid acquisition

Delay Product Hunt until the product has a reliable first-Ghost experience, a strong demonstration, at least five permissioned quotes, and a small honest user base ready to participate. Delay paid ads until activated-player retention is credible. Early spending is better used on observed testing or modest creator production than on clicks.

## 6. Analytics and experiment design

> **Correction from the analytics audit**
>
> Do not add `target_captured` or `ghost_attempt_graded`. Those concepts already exist as `blunder_recorded` / `blunder_added_manual` and `srs_review_recorded`. The only immediate gaps are exact target arrival and reliable return context.

### Current event inventory

Ghost Replay already has server-side product events for game starts and endings, automatic and manual target capture, opponent moves, review grading, and drills. The frontend also initializes PostHog with SPA page views, autocapture, Do Not Track support, and session recording disabled. <sup>[5, 6, 7, 8]</sup>

| **Milestone**                           | **Canonical event**                                | **Status**      | **Use**                                                                                   |
|-----------------------------------------|----------------------------------------------------|-----------------|-------------------------------------------------------------------------------------------|
| **Landing**                             | `$pageview`                                     | Exists          | Use for source and landing-page denominator.                                              |
| **Game intent**                         | `game_started`                                   | Exists          | Canonical start event; enrich rather than replace.                                        |
| **Game completion**                     | `game_ended`                                     | Exists          | Use result, rating, ply, and completion diagnostics.                                      |
| **Automatic target capture**            | `blunder_recorded`                               | Exists          | Includes evaluation loss and opening family.                                              |
| **Manual target capture**               | `blunder_added_manual`                           | Exists          | Use alongside automatic capture; add comparable properties only when available.           |
| **Ghost steering**                      | `opponent_move_served` + `has_target_blunder=true` | Exists          | An intermediary/proxy; it does not prove the exact target position was reached.           |
| **Exact target reached / review armed** | `ghost_position_reached`                         | Missing         | Add at the code path where target FEN is confirmed and review is armed.                   |
| **Review graded**                       | `srs_review_recorded`                            | Exists          | Includes pass, streak, evaluation delta, and related review data.                         |
| **Return / second product session**     | Properties on `game_started`                     | Missing context | Measure game number and elapsed time without a parallel `second_session_started` event. |

### Canonical funnel mapped to existing names

> **Do not introduce a parallel event taxonomy.**

```text
$pageview
-> game_started
-> blunder_recorded OR blunder_added_manual
-> game_ended
-> game_started [return context / game_number >= 2]
-> opponent_move_served [has_target_blunder = true]  # steering only
-> ghost_position_reached                            # exact arrival
-> srs_review_recorded                               # graded outcome
```

### The two immediate instrumentation gaps

#### A. Exact Ghost arrival

Add `ghost_position_reached` at the exact code path where the current board FEN matches the stored target and the review becomes armed. Emit it once per session-target pair. Do not infer arrival from an opponent move merely having `has_target_blunder=true`; steering can begin without the target ever being reached.

| **Property**                  | **Purpose**                                                             |
|-------------------------------|-------------------------------------------------------------------------|
| **blunder_id**                | Stable target identifier.                                               |
| **session_id / game_id**      | Deduplication and sequence reconstruction.                              |
| **review_context**            | `natural_game`, `immediate_replay`, `seeded_demo`, or `drill`.  |
| **opening_family**            | Segmentation and content insight.                                       |
| **steering_plies**            | How many opponent plies were used to reach the target, when applicable. |
| **hours_since_capture**       | Time from target creation to exact arrival.                             |
| **reached_via_transposition** | Optional diagnostic if equivalent position paths matter.                |

#### B. Return context on the existing game start

Enrich `game_started` rather than adding a parallel “second session” event. The returned-player definition can then be queried consistently for the second game, a later browser session, or a later calendar day.

| **Property**                      | **Purpose**                                                                      |
|-----------------------------------|----------------------------------------------------------------------------------|
| **game_number**                   | Lifetime completed/started game ordinal for the user.                            |
| **hours_since_previous_game**     | Supports same-session versus later-session analysis.                             |
| **is_returning_player**           | True when a prior game exists; define and document the rule.                     |
| **previous_game_completed**       | Distinguishes return after completion from recovery/restart.                     |
| **first_touch_source / campaign** | Persist acquisition source so server events can be tied to the original channel. |

> **Small related enhancement**
>
> Add `review_context` to the existing `srs_review_recorded` properties so the same grading event can distinguish natural-game reviews from immediate or seeded bridge reviews. This is a property extension, not a new grading event.

### Dashboards and analyses

| **Dashboard**   | **Questions answered**                                                                                                 |
|-----------------|------------------------------------------------------------------------------------------------------------------------|
| **Acquisition** | Visitors, game starters, target owners, bridge activations, core activations, and retained players by source/campaign. |
| **Activation**  | Median time to target capture, exact arrival, and first graded review; bridge versus natural context.                  |
| **Steering**    | Targeted opponent moves served versus exact targets reached; steering plies; abandonment before arrival.               |
| **Retention**   | 7-day and 28-day return by no-target, captured-only, reached-but-ungraded, failed-review, and passed-review cohorts.   |
| **Quality**     | Game completion, engine errors, duplicate events, pass/fail distribution, and evaluation-delta integrity.              |
| **Content fit** | Opening families and mistake types associated with review completion and return.                                       |

### Metrics that matter

- Landing-to-game-start rate.

- First-game completion rate.

- Share of players creating an automatic or manual target.

- Share of target owners who reach the exact target.

- Share who complete a graded bridge review and a graded natural review.

- Median time from capture to exact arrival and review.

- Second-game, 7-day, and 28-day return rates.

- Activated players by source and campaign.

- Games per retained player per week.

- Share rate after a passed Ghost review, once sharing exists.

> **Canonical weekly question**
>
> Which source created the most players who completed a graded Ghost review, and what percentage of those players returned?

## 7. Qualitative research

Behavioral events show what happened; they rarely explain the player’s expectation, confusion, or willingness to change habits. Pair the event funnel with ten observed sessions during the first month: five in Week 1 and five in Weeks 2-4.

### Observed-session protocol

1.  Ask the participant to share the screen and think aloud.

2.  Begin on the public landing page; do not explain the product before they interpret it.

3.  Avoid rescuing the participant unless they are completely blocked.

4.  Record timestamps for misunderstanding, hesitation, delight, and abandonment.

5.  After the session, separate usability problems from missing-value problems.

### Interview questions

- Before you started, what did you think Ghost Replay was going to do?

- At what point did the product become interesting?

- What did you expect to happen after your first game?

- Did you know whether a Ghost had been captured?

- What would make you come back tomorrow?

- What do you use today to avoid repeating the same mistake?

- Which part would you miss if Ghost Replay disappeared?

- Who else do you know who has this problem?

### In-product feedback

> **How useful did it feel to face this position again?  
>   
> Optional follow-up: What would have made it more useful?**

Trigger this only after the first graded review. Keep broad session recording disabled during the early stage. Use event analytics plus consented observed sessions, explain what game/product data is collected, and provide a clear deletion route.

## 8. Sharing and re-engagement loops

### A shareable learning object

After a player passes a Ghost, offer a result that remains useful to the recipient: a clean board image, short animation, or public position link that asks the recipient to choose a move before revealing the story.

> **I defeated a Caro-Kann ghost from one of my old games. Can you find the move?**

- Reveal the move choice before explaining Ghost Replay.

- Make the shared page playable without signup.

- Explain that the position came from a prior mistake that returned inside a later game.

- Exclude usernames, opponents, and full game histories by default; sharing must be explicit.

- Instrument sharing only after the feature exists; avoid speculative event clutter.

### Re-engagement after value

Prompt account claiming or optional reminders after a player captures or defeats a Ghost. The message should name the benefit - for example, “Keep your ghosts across devices” or “Tell me when a ghost is due” - rather than using a generic registration request. Test reminders only after there is evidence that due targets predict return.

## 9. Monetization learning

The purpose of early analytics is not to identify where to place a paywall. It is to identify repeated, valuable jobs for which players or coaches may pay. Usage can reveal importance, but willingness to pay requires a concrete offer.

| **Observed behavior or request**               | **Candidate paid product**                                      |
|------------------------------------------------|-----------------------------------------------------------------|
| **Players repeatedly paste external games**    | Automatic Chess.com/Lichess synchronization.                    |
| **Players use multiple devices**               | Claimed account and cross-device history.                       |
| **Players accumulate many Ghosts**             | Advanced filtering, organization, history, and review planning. |
| **Players ask when Ghosts are due**            | Email, browser, or mobile reminders.                            |
| **Players inspect trends repeatedly**          | Deeper personal analytics and reports.                          |
| **Coaches bring multiple students**            | Coach dashboard, assignments, and student progress.             |
| **Clubs adopt the workflow**                   | Club/classroom management and shared curricula.                 |
| **Advanced players request deeper analysis**   | Premium engine depth, analysis throughput, or limits.           |
| **Players mainly want to support the project** | Supporter membership with cosmetic recognition.                 |

### Test willingness with concrete propositions

> **Automatic import and cross-device history would cost $X per month. Would you use that, continue with the free manual version, or stop using Ghost Replay?**

Ask only after repeated use - for example, four or more sessions or several completed reviews. Keep the core Ghost training loop free initially. The most plausible first paid offers are convenience, deeper analysis, or coach workflows.

### What not to do yet

- Do not buy ads before activated-player retention is credible.

- Do not use registrations or anonymous accounts as the primary success metric.

- Do not lead with Opening Drills or “AI-powered.”

- Do not launch publicly before a newcomer can understand the Ghost mechanism quickly.

- Do not open a community server merely because products are expected to have one.

- Do not add a large feature backlog before watching ten people onboard.

- Do not infer willingness to pay from feature usage alone.

## 10. The first seven days

This checklist deliberately narrows analytics work to the two verified gaps and begins recruitment early enough for the Week 1 exit criteria to be achievable.

| **Timing**  | **Action**                                                                                                              |
|-------------|-------------------------------------------------------------------------------------------------------------------------|
| **Day 1**   | Rewrite the landing-page hierarchy and make automatic-capture language precise.                                         |
| **Day 1-2** | Create the 45-second mistake-return-correction demonstration.                                                           |
| **Day 2**   | Add `ghost_position_reached` at exact target-FEN arrival, emitted once per session-target pair.                       |
| **Day 2-3** | Enrich `game_started` with return context; add `review_context` to existing `srs_review_recorded` if inexpensive. |
| **Day 3**   | Build a funnel dashboard using existing event names plus the new exact-arrival event.                                   |
| **Day 3-4** | Add a seeded sample or immediate-replay bridge so a first-time player can understand the concept quickly.               |
| **Day 4**   | Add title, description, social metadata, branded favicon, and update the README opening.                                |
| **Day 1-5** | Recruit ten appropriate rapid players; schedule five observed sessions.                                                 |
| **Day 5-7** | Observe five sessions, score the Week 1 exit criteria, and name the single largest blocker.                             |
| **Day 7**   | Decide: proceed to the Founding 25 cohort, repeat Week 1 with another fix, or narrow the target audience.               |

### Week 1 scorecard

| **Measure**                                           | **Target**         | **Purpose**   |
|-------------------------------------------------------|--------------------|---------------|
| **Observed sessions completed**                       | 5                  | Required      |
| **Can explain Ghost mechanism after landing/demo**    | 4 of 5             | Understanding |
| **Complete a graded bridge review within 20 minutes** | 3 of 5             | Activation    |
| **Exact-arrival event visible without duplication**   | 100% of test cases | Measurement   |
| **Largest blocker stated as one testable hypothesis** | 1 clear hypothesis | Learning      |

> **Recommended next decision**
>
> If at least three observed players complete a graded bridge review and can explain why the experience matters, recruit the remaining fifteen Founding Players. If not, fix the first experience before expanding acquisition.

## Appendix A: Outreach and launch templates

### Founding Player outreach

> **I’m testing a free chess trainer for players who keep recognizing a mistake only after they make it again. Instead of only showing an analysis or isolated puzzle, Ghost Replay can bring the position back inside a later game. I’m looking for 25 online rapid players who will play three sessions during the next seven days and spend ten minutes telling me what is confusing. There is no payment, upsell, or required signup. Would you be interested?**

### Coach outreach

> **I built a free trainer that saves a student’s mistake and can bring the position back inside a later game. I’m trying to learn whether that is useful for recurring opening or early-middlegame errors. Would you try it with one student and give me 15 minutes of feedback? I’m especially interested in what you would need to assign targets and see whether the student later handled them correctly.**

### Creator outreach

> **Send me one game with an instructive mistake. I’ll turn it into a short “mistake returns as a ghost” demonstration that teaches the position before it mentions the product. You can use the clip whether or not you promote Ghost Replay.**

### r/chess post outline

- Disclose immediately that you built Ghost Replay.

- Explain the personal problem that motivated it.

- Embed the 45-second demonstration in the post.

- Explain the mechanism and current limitations honestly.

- Ask one focused question, such as whether the return felt meaningfully different from a puzzle.

- Use a clean product link; do not include tracking parameters in rule citations.

- Before posting, verify current rules and account eligibility; contact moderators when uncertain.

### Show HN outline

- What it is in one factual sentence.

- Why you built it and what was unsatisfying about ordinary review.

- How target steering and review work at a high level.

- What is technically unusual or difficult.

- What currently does not work or remains experimental.

- A direct, working demo with no signup barrier.

- One sincere request for technical or product feedback; no coordinated voting.

## Appendix B: Experiment backlog

| **Priority**  | **Experiment**                       | **Question**                                                                                       |
|---------------|--------------------------------------|----------------------------------------------------------------------------------------------------|
| **Very high** | Guaranteed sample / immediate replay | Does a guaranteed first Ghost increase graded bridge activation and correct product understanding? |
| **Very high** | Hero rewrite + short demo            | Does benefit-first messaging improve qualified game starts and explanation accuracy?               |
| **Very high** | Exact arrival + return context       | Can Ghost activation and later return be measured without proxy events?                            |
| **High**      | Visible Ghost state                  | Does showing captured/due status reduce uncertainty and increase second-game starts?               |
| **High**      | Manual PGN paste                     | Does importing one loss shorten time to a personal target and attract stronger users?              |
| **Medium**    | Shareable position challenge         | Do passed reviews create qualified referral traffic and activations?                               |
| **Medium**    | Due reminder opt-in                  | Does re-engagement help only after a due target exists?                                            |
| **Later**     | Automatic account integrations       | Build after repeated manual-import demand.                                                         |
| **Not now**   | Paid ads                             | Wait until activation and retention are stable enough to interpret spend.                          |

## Sources and verification notes

Product and analytics claims were checked against the current Ghost Replay repository. Community policies were checked against canonical rules or official guidance on 11 August 2026. Because community rules are operationally volatile, reverify them immediately before posting. Links below intentionally contain no tracking parameters.

**[1]** [Ghost Replay SPEC.md](https://github.com/guelo/ghostreplay/blob/master/SPEC.md). Core loop, automatic capture behavior, anonymous accounts, and target-position arming behavior.

**[2]** [Ghost Replay landing page source (src/App.tsx)](https://github.com/guelo/ghostreplay/blob/master/src/App.tsx). Current two-product hero and “Every costly move” landing-page language.

**[3]** [Ghost Replay public HTML (index.html)](https://github.com/guelo/ghostreplay/blob/master/index.html). Current title, default Vite favicon, and metadata baseline.

**[4]** [Ghost Replay README](https://github.com/guelo/ghostreplay/blob/master/README.md). Current repository presentation and Milestone 1 scaffold wording.

**[5]** [Automatic and manual target events (backend/app/api/blunder.py)](https://github.com/guelo/ghostreplay/blob/master/backend/app/api/blunder.py). `blunder_recorded` and `blunder_added_manual`, including capture properties.

**[6]** [SRS review event (backend/app/api/srs.py)](https://github.com/guelo/ghostreplay/blob/master/backend/app/api/srs.py). `srs_review_recorded` and its pass, streak, evaluation-delta, and review properties.

**[7]** [Game event taxonomy (backend/app/api/game.py)](https://github.com/guelo/ghostreplay/blob/master/backend/app/api/game.py). `game_started`, `game_ended`, and `opponent_move_served` with `has_target_blunder`.

**[8]** [Frontend PostHog setup (src/analytics/posthog.ts)](https://github.com/guelo/ghostreplay/blob/master/src/analytics/posthog.ts). SPA page views, autocapture, Do Not Track handling, and disabled session recording.

**[9]** [r/chess community rules](https://www.reddit.com/r/chess/about/rules/). Current participation and limited self-promotion requirements; recheck before posting.

**[10]** [r/chessbeginners community rules](https://www.reddit.com/r/chessbeginners/about/rules/). Current prohibition on advertising a chess website made by the poster; recheck before posting.

**[11]** [r/Chesscom community rules](https://www.reddit.com/r/Chesscom/about/rules/). Current restrictions on promoting other platforms and self-promotion; recheck before posting.

**[12]** [Show HN guidelines](https://news.ycombinator.com/showhn.html). Eligibility, tryability, signup barriers, and participation expectations.

**[13]** [Hacker News moderator launch guidance](https://news.ycombinator.com/item?id=22336638). Factual/personal tone, avoiding marketing language, and current posting guidance.

**[14]** [Google: JavaScript SEO basics](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics). Server-side or pre-rendering benefits and crawler compatibility.

**[15]** [Google: Control your snippets in search results](https://developers.google.com/search/docs/appearance/snippet). Descriptive meta descriptions and search-result snippets.

### Document status

> **Ready to operate**
>
> This plan is intended to be updated weekly. Preserve the canonical analytics taxonomy, record decisions and observed evidence, and revise channel guidance whenever source rules change.
