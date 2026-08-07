# statsbadge-bluesky

Bluesky accounts and feeds, for [statsbadge](https://github.com/pimoroni/statsbadge).

Watch as many handles and feeds as you like. Each one is offered by name under a single **Bluesky** heading in the page picker, so a page can take the latest post from one account, a follower count from another and the newest thing in a feed, all on the same screen.

Public APIs only. There is no login, no app password and nothing to keep secret - and so nothing here that anybody could not already see on the web.

## Install

```bash
statsbadge ext add bluesky
```

Then, in the config UI under **Extensions**, list the handles and feeds you want.

## Settings

| Setting | What it does |
| ------- | ------------ |
| Handles | Accounts to watch, separated by commas. The `@`, and a profile URL pasted whole, are both understood |
| Feeds | Feeds to watch, separated by commas. A feed's page on bsky.app, or the `at://` URI it is published under |
| Ask every | Seconds between refreshes. 120 by default |
| Pictures | `off`, `small` or `large` - see below |

Each entry becomes a separate source in the picker:

```
Bluesky
    gadgetoid.com
    pimoroni.com
    Raspberry Pi Pico
    Mechanical Keyboards
```

An account is listed by the handle you typed. A feed is listed by its display name once the AppView has been asked for one - until then, by the last part of its URI.

## What each one reports

An **account**:

| Reading | What it is |
| ------- | ---------- |
| Latest post | Its newest, reposts excluded |
| Latest reply | The newest reply somebody else left on one of its recent posts |
| Followers | |
| Following | |
| Posts | |
| Likes on the latest | |
| Reposts of the latest | |
| Replies to the latest | |
| Quotes of the latest | |

A **feed**:

| Reading | What it is |
| ------- | ---------- |
| Latest post | The newest post in it |
| Likes on the feed | How many people have liked the feed itself |

Latest post and Latest reply are messages. They go on a **Notifications** page as a block of text with the sender and how long ago. The rest are numbers and go anywhere a number goes.

Followers, following and posts are kept **hourly**, so a graph of any of them shows a week rather than the last ninety seconds. The AppView reports no history, so that ring is built as the host runs: it is empty on a first launch and fills an hour at a time.

## Pictures

One picture per post, where the post has one: its images, or the picture on a link card, cropped to what is actually in it and drawn in the theme's palette.

`small` is 64x48 in four shades and adds about 950 bytes to a message; `large` is 128x96 in eight and adds about 4KB. Four subsequent posts with images might cause animation hiccups so choose carefully.

## Notes

Everything comes from the public AppView at `public.api.bsky.app`, which serves profiles, author feeds, threads and feed generators to anyone.

**How many requests.** An account costs two, plus one for each thread opened looking for a reply - up to five. A feed costs two. So four accounts and two feeds is at most twenty-four requests every couple of minutes, which is why the interval has a floor. Watch fewer things or ask less often if that matters to you.

**Replies stand in for mentions.** Notifications need a login and post search needs one too, so the threads under an account's posts are the only public place a mention can come from. Up to three are opened, newest first, and a reply from the account itself is skipped - a thread somebody is carrying on alone is not an answer.

**One bad entry does not cost the rest.** A handle spelled wrong is reported on the config page while every other account and feed keeps reporting.

A repost carries no words of its own, so the post itself is drawn with "reposted by" beside it. Reposts are excluded from an account's **Latest post**, since the numbers beside it would be a stranger's.

A post can carry no words at all - a bare link, or a bare picture. The link card's headline, or the image's alt text, is what it shows in that case. Bodies are cut to 160 characters on the server, comfortably more than the two or three lines a page draws.

Bluesky "readings" change every couple of minutes and the badge polls every second, so they are declared slow: the host sends them when they change and the badge holds on to them.
