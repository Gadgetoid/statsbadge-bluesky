# statsbadge-bluesky

A Bluesky account, its posts and a feed, for [statsbadge](https://github.com/pimoroni/statsbadge).

The account's newest post, the newest reply to it and the newest post in a feed you name, alongside followers, following, posts and how the last post has done. Add a **Notifications** page and put any mixture of them on it: the messages stack down the page and the counters go in a strip along the bottom.

Public APIs only. There is no login, no app password and nothing to keep secret - and so nothing here that anybody could not already see on the web.

## Install

```bash
statsbadge ext add bluesky
```

Then, in the config UI under **Extensions**, set the handle to watch.

## Settings

| Setting | What it does |
| ------- | ------------ |
| Handle | The account to watch, like `gadgetoid.com`. The `@`, and a profile URL pasted whole, are both understood |
| Feed | Optional. A feed's page on bsky.app, or the `at://` URI it is published under |
| Ask every | Seconds between refreshes. 120 by default |
| Pictures | `off`, `small` or `large` - see below |

## Pictures

One picture per post, where the post has one: the images it carries, or the picture on a link card, cropped to what is actually in it and drawn in the theme's palette.

`small` is 64x48 in four shades and adds about 950 bytes to a message; `large` is 128x96 in eight and adds about 4KB. Four subsequent posts with images might cause animation hiccups so choose carefully.

## What it reports

Three of these are messages, and go on a Notifications page as a block of text with who it is from and how long ago:

| Reading | What it is |
| ------- | ---------- |
| Latest post | The account's newest, reposts excluded |
| Latest reply to you | The newest reply somebody else left on one of its recent posts |
| Latest in the feed | The newest post in the feed you named |

The rest are numbers, and go anywhere a number goes:

| Reading | What it is |
| ------- | ---------- |
| Followers | |
| Following | |
| Posts | |
| Likes on your latest | |
| Reposts of your latest | |
| Replies to your latest | |
| Quotes of your latest | |
| Likes on the feed | How many people have liked the feed itself |

Followers, following and posts are kept **hourly**, so a graph of any of them shows a week rather than the last ninety seconds. The AppView reports no history of its own, so that ring is built as the host runs: it is empty on a first launch and fills an hour at a time.

## Notes

Everything comes from the public AppView at `public.api.bsky.app`, which serves profiles, author feeds, threads and feed generators to anyone. Three requests a refresh, or up to seven where there are threads to read and a feed configured.

**Replies stand in for mentions.** Notifications need a login and post search is refused without one, so the threads under the account's own posts are the only public place a mention can come from. Up to three are opened, newest first, and the account's own replies do not count - a thread somebody is carrying on alone is not somebody answering.

A repost carries no words of its own, so the post itself is drawn with "reposted by" beside it. Reposts are excluded from **Latest post**, since the numbers beside it would be a stranger's.

A post can carry no words at all - a link on its own, or a picture on its own. The link card's headline, or the image's alt text, is what it shows in that case. Bodies are cut to 160 characters on the server, comfortably more than the two or three lines a page draws.

Bluesky "readings" change every couple of minutes and the badge polls every second, so they are declared slow: the host sends them when they change and the badge holds on to them.
