"""A Bluesky account, its posts and a feed, for a badge to show on a notifications page.

Everything comes from the public AppView at `https://public.api.bsky.app/xrpc`, which serves
these unauthenticated. No account, no app password, nothing to keep secret - the cost being
that only what anyone could see is here.

    com.atproto.identity.resolveHandle    a handle to the DID a feed URI is built from
    app.bsky.actor.getProfile             the counters, and who this is
    app.bsky.feed.getAuthorFeed           the newest post, and its own numbers
    app.bsky.feed.getPostThread           the newest reply to it, which is the public
                                          half of a mention: notifications need a login
    app.bsky.feed.getFeed                 the newest post in a feed
    app.bsky.feed.getFeedGenerator        that feed's name and how many like it

The messages travel as the shape a `notify` page draws - who it is from, what it says, how
long ago, and a word about why - which is the same shape a Mastodon post or an RSS entry has,
so none of this is Bluesky-specific by the time it reaches the badge.
"""

import base64
import datetime
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from statsbadge import imaging
from statsbadge.sources.base import Source

APPVIEW = "https://public.api.bsky.app/xrpc"

# How often the AppView is asked, unless the setting says otherwise. A timeline is not a
# sensor, and three to five requests every two minutes is nothing to a public service.
DEFAULT_EVERY = 120.0
MIN_EVERY = 30.0
MAX_EVERY = 3600.0
RETRY_AFTER = 60.0
FETCH_POLL = 1.0

# How far back to read the author's feed. Enough to find one post of theirs that somebody has
# replied to, on an account that reposts a good deal.
FEED_SCAN = 25

# How many of those threads to open looking for a reply from somebody else. More than one
# because a thread whose only reply is the author carrying on their own thought is common,
# and few because each is a request.
REPLY_SCAN = 3

# A post is three hundred characters and the page draws two or three lines of it. Cut here
# rather than on the badge: the rest is neither drawn nor worth sending every time it changes.
TEXT_MAX = 160

# The counters, kept once an hour so a graph of them says something. The AppView reports no
# history of its own, so this is the only place one can come from - which means a ring starts
# empty and fills as the host runs.
HISTORY_EVERY = 3600.0
HISTORY_POINTS = 48
HISTORY_MS = int(HISTORY_EVERY * 1000)
COUNTS = "counts"
WHO = "who"

# What this says when it has not been given an account. Not counted as a fault - an extension
# nobody has configured is not broken - but worth showing, since a silent source that reports
# nothing looks the same as one that is not installed.
UNSET = "no handle set"

# Which preset a setting asks for. Landscape either way: the page puts a picture down the left
# of the words, and a tall one beside two lines of text is a column of nothing.
PRESETS = {"small": "low", "large": "high"}
# How many decoded pictures to remember, keyed by the post they came from: the same few posts
# are refetched every couple of minutes and nothing about them changed.
IMAGE_CACHE = 12

GROUP = "bluesky"

FIELDS = {
    "latest": {"label": "Latest post", "item": True},
    "reply": {"label": "Latest reply to you", "item": True},
    "feed": {"label": "Latest in the feed", "item": True},
    "followers": {"label": "Followers", "history": True},
    "following": {"label": "Following", "history": True},
    "posts": {"label": "Posts", "history": True},
    "likes": {"label": "Likes on your latest"},
    "reposts": {"label": "Reposts of your latest"},
    "replies": {"label": "Replies to your latest"},
    "quotes": {"label": "Quotes of your latest"},
    "feed_likes": {"label": "Likes on the feed"},
}


class Bluesky(Source):
    name = "bluesky"
    label = "Bluesky"

    settings = (
        {"key": "handle", "label": "Handle", "type": "text",
         "hint": "The account to watch, like gadgetoid.com or someone.bsky.social - no @"},
        {"key": "feed", "label": "Feed", "type": "text",
         "hint": "Optional. A feed's page on bsky.app, or the at:// URI it is published "
                 "under. Leave it empty for no feed"},
        {"key": "every", "label": "Ask every", "type": "number",
         "default": int(DEFAULT_EVERY), "unit": "seconds",
         "min": int(MIN_EVERY), "max": int(MAX_EVERY), "step": 30},
        {"key": "images", "label": "Pictures", "type": "choice",
         "options": ["off", "small", "large"], "default": "small",
         "hint": "One picture per post, cropped and drawn in the theme's palette"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        # What the fetcher last brought back, and the hourly counter rings. Both are replaced
        # on the fetcher's thread and read while sampling, so both go through the lock.
        self._readings = {}
        self._images = {}
        self._counts = {}
        self._counts_at = None
        self._lock = threading.Lock()
        self._next = 0.0
        self._next_history = 0.0
        # Trouble with the configured feed, which is reported without the account's own
        # readings being lost to it.
        self._feed_fault = None
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Take up the rings the last run kept, then fetch on a thread of its own.

        Nothing in `sample` may wait on a network: every source shares the collector's
        thread and the first sample is taken while the server is still starting up.
        """
        kept = self.store.get(COUNTS) or {}
        with self._lock:
            self._counts = {name: list(points) for name, points in kept.items()
                            if isinstance(points, list)}
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-bluesky")
            self._fetcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._fetcher is not None:
            self._fetcher.join(timeout=2.0)
            self._fetcher = None

    def configure(self, settings):
        """Take settings while running, and ask again rather than waiting out the interval."""
        super().configure(settings)
        was = self.handle
        self._read_settings()
        if self.last_fault == UNSET and self.handle:
            # That message was about the settings, and they have just been given. Waiting for
            # a fetch to succeed before withdrawing it leaves the config page saying no handle
            # is set for as long as the first few requests take.
            self.last_fault = None
        if was != self.handle:
            # A different account's readings are not this one's, and neither are its pictures.
            with self._lock:
                self._readings = {}
            self._images = {}
        self._next = 0.0
        self._wake.set()

    def _read_settings(self):
        self.handle = _handle(self.config.get("handle"))
        self.feed = str(self.config.get("feed") or "").strip()
        try:
            every = float(self.config.get("every") or DEFAULT_EVERY)
        except (TypeError, ValueError):
            every = DEFAULT_EVERY
        self.every = max(MIN_EVERY, min(MAX_EVERY, every))
        wanted = str(self.config.get("images") or "small")
        # Off where the extra is not installed, rather than a fault on every fetch: a host
        # with no decoder should show the words and say nothing about it.
        self.preset = PRESETS.get(wanted)
        # One group, and slow: a timeline fetched every two minutes has no business in a frame
        # the badge collects every second.
        self.groups = {GROUP: {"label": "Bluesky", "slow": True, "fields": dict(FIELDS)}}
        self.provides = (GROUP,)

    # -- sampling -----------------------------------------------------------

    def sample(self, frame, dt):
        """Whatever the fetcher last brought back. Nothing here touches the network."""
        with self._lock:
            readings = dict(self._readings)
        if readings:
            frame[GROUP] = readings

    def series(self):
        """The counter rings, on the hour they are kept at.

        The collector would sample these at its own rate, and ninety seconds of a follower
        count is a flat line. An hour apart is the shape of a week.
        """
        with self._lock:
            counts = {name: list(points) for name, points in self._counts.items()}
            at = self._counts_at
        if not counts or at is None:
            return {}
        age_ms = max(0, int((time.monotonic() - at) * 1000))
        return {f"{GROUP}.{name}": {"points": points, "every_ms": HISTORY_MS,
                                    "age_ms": age_ms}
                for name, points in counts.items() if points}

    def note_fault(self, exc):
        """What the AppView said, without a type name in front of it."""
        if isinstance(exc, BlueskyError):
            self.faults += 1
            self.last_fault = str(exc)
            return
        super().note_fault(exc)

    # -- fetching -----------------------------------------------------------

    def _fetch_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as exc:
                # The fetcher must not die, or the timeline would stand at whatever it last
                # was with nothing ever replacing it.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _refresh(self):
        if not self.handle:
            # Not a fault: an extension nobody has given an account to is unconfigured, and
            # counting that would report a broken source on every host that installed it.
            self.last_fault = UNSET
            return
        if time.monotonic() < self._next:
            return
        self._feed_fault = None
        try:
            readings = self._fetch()
        except Exception as exc:
            self._next = time.monotonic() + RETRY_AFTER
            self.note_fault(exc)
            return
        with self._lock:
            self._readings = readings
        self._keep_counts(readings)
        self._next = time.monotonic() + self.every
        self.note_ok()
        if self._feed_fault is not None:
            # After note_ok, which is what clears a fault: the account's readings stand and
            # the feed somebody typed in is still wrong.
            self.note_fault(self._feed_fault)

    def _fetch(self):
        profile = self._get("app.bsky.actor.getProfile", actor=self.handle)
        readings = {
            "followers": profile.get("followersCount"),
            "following": profile.get("followsCount"),
            "posts": profile.get("postsCount"),
        }
        did = profile.get("did")
        self.store.set(WHO, {"did": did,
                             "name": profile.get("displayName") or profile.get("handle")})

        feed = self._get("app.bsky.feed.getAuthorFeed", actor=self.handle,
                         limit=FEED_SCAN, filter="posts_no_replies").get("feed") or ()
        # Their own newest, reposts excluded: a repost carries somebody else's numbers, and
        # "likes on your latest" would be a stranger's.
        mine = next((entry for entry in feed if not entry.get("reason")), None)
        if mine:
            post = mine["post"]
            readings["latest"] = self._with_picture(_post_item(mine), post)
            readings["likes"] = post.get("likeCount")
            readings["reposts"] = post.get("repostCount")
            readings["replies"] = post.get("replyCount")
            readings["quotes"] = post.get("quoteCount")
            reply = self._newest_reply(feed, did)
            if reply is not None:
                item = _post_item(reply)
                # Every one of these is a reply, so saying so beside the name adds nothing.
                item["note"] = None
                readings["reply"] = self._with_picture(item, reply.get("post"))

        if self.feed:
            readings.update(self._fetch_feed(did))
        return readings

    def _newest_reply(self, feed, did):
        """The newest reply somebody else left on one of their recent posts.

        The public AppView has no notifications and refuses post search without a login, so
        the threads under their own posts are where a mention has to come from. Their own
        replies do not count: a thread they are carrying on alone is not somebody answering.
        """
        answered = [entry for entry in feed
                    if not entry.get("reason") and (entry["post"].get("replyCount") or 0)]
        for entry in answered[:REPLY_SCAN]:
            thread = self._get("app.bsky.feed.getPostThread",
                               uri=entry["post"]["uri"], depth=1).get("thread") or {}
            replies = [reply for reply in (thread.get("replies") or ())
                       if reply.get("post")
                       and reply["post"].get("author", {}).get("did") != did]
            if replies:
                return max(replies, key=lambda reply: _at(reply["post"]) or "")
        return None

    def _fetch_feed(self, owner):
        """The newest post in the configured feed, and how many people like the feed itself.

        A feed that cannot be read is worth reporting - somebody typed it in - but not worth
        losing the account's own readings over, so it is caught here and reported after them.
        """
        try:
            uri = self._feed_uri(owner)
            posts = self._get("app.bsky.feed.getFeed", feed=uri, limit=1).get("feed") or ()
            about = self._get("app.bsky.feed.getFeedGenerator", feed=uri).get("view") or {}
        except Exception as exc:
            self._feed_fault = exc
            return {}
        found = {"feed_likes": about.get("likeCount")}
        if posts:
            # Named after the feed rather than after whoever posted: on a page beside your own
            # timeline, which feed it came out of is the thing that is not obvious.
            item = _post_item(posts[0])
            item["note"] = about.get("displayName") or "feed"
            found["feed"] = self._with_picture(item, posts[0].get("post"))
        return found

    def _feed_uri(self, owner):
        """The at:// URI of the configured feed, from either way of writing one.

        A feed is shared as its page on bsky.app - `/profile/<handle>/feed/<name>` - and
        published as `at://<did>/app.bsky.feed.generator/<name>`. Pasting the first is what
        anybody will do, and the handle in it has to be resolved: an at:// URI names a DID.
        """
        if self.feed.startswith("at://"):
            return self.feed
        parts = urllib.parse.urlsplit(self.feed if "//" in self.feed else f"//{self.feed}")
        crumbs = [crumb for crumb in parts.path.split("/") if crumb]
        if len(crumbs) >= 4 and crumbs[0] == "profile" and crumbs[2] == "feed":
            who, name = crumbs[1], crumbs[3]
            did = (owner if who == self.handle
                   else self._get("com.atproto.identity.resolveHandle",
                                  handle=who).get("did"))
            return f"at://{did}/app.bsky.feed.generator/{name}"
        raise BlueskyError(f"cannot tell what feed {self.feed!r} is")

    def _with_picture(self, item, post):
        """`item` with one picture on it, where the post has one and the setting wants it.

        One per post: a page has room for one, and the first image is the one the author led
        with. A picture that will not fetch or will not decode is left off - the words are the
        post, and a message with no picture is a smaller message rather than a failure worth
        reporting.
        """
        if item is None or not self.preset or not post:
            return item
        url = _thumbnail(post.get("embed"))
        if not url:
            return item
        # Keyed on the preset too, so changing the setting is not a page of the old size until
        # every post happens to change.
        key = f"{self.preset}:{url}"
        if key not in self._images:
            made = None
            try:
                with urllib.request.urlopen(url, timeout=15) as response:
                    raw = response.read()
                made = base64.b64encode(
                    imaging.thumbnail(raw, self.preset, "landscape")).decode("ascii")
            except Exception:
                made = None
            if len(self._images) >= IMAGE_CACHE:
                self._images.clear()
            self._images[key] = made
        picture = self._images.get(key)
        if picture:
            item = dict(item)
            item["image"] = picture
        return item

    def _keep_counts(self, readings):
        """Append this hour's counters to their rings, if an hour has gone by.

        On the wall clock rather than on how often the fetcher runs, so the spacing the badge
        is told about is the spacing the points are really on.
        """
        now = time.monotonic()
        if now < self._next_history:
            return
        self._next_history = now + HISTORY_EVERY
        with self._lock:
            for name in ("followers", "following", "posts"):
                value = readings.get(name)
                if value is None:
                    continue
                ring = self._counts.setdefault(name, [])
                ring.append(int(value))
                del ring[0:max(0, len(ring) - HISTORY_POINTS)]
            self._counts_at = now
            kept = {name: list(points) for name, points in self._counts.items()}
        self.store.set(COUNTS, kept)

    # -- talking to it ------------------------------------------------------

    def _get(self, method, **params):
        query = urllib.parse.urlencode({key: value for key, value in params.items()
                                        if value is not None})
        request = urllib.request.Request(f"{APPVIEW}/{method}?{query}",
                                         headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The status alone says nothing useful: a handle that does not exist and a handle
            # spelled with an @ are both 400, and the body says which.
            detail = ""
            try:
                said = json.loads(exc.read().decode("utf-8")) or {}
                detail = said.get("message") or said.get("error") or ""
            except Exception:
                detail = ""
            raise BlueskyError(f"HTTP {exc.code}"
                               + (f": {detail}" if detail else "")) from exc


class BlueskyError(Exception):
    """What the AppView said was wrong, as one line for the config UI to show."""


# -- turning a post into a message ------------------------------------------

def _handle(given):
    """A handle as the AppView wants it: no @, no https://, no trailing path.

    People paste their profile URL and people type the @ they see on the page, and both are
    a 400 from an endpoint that wanted `gadgetoid.com`.
    """
    text = str(given or "").strip().lstrip("@")
    if "/" in text:
        text = text.replace("https://", "").replace("http://", "")
        crumbs = [crumb for crumb in text.split("/") if crumb]
        # bsky.app/profile/<handle>, and anything else keeps its last part.
        text = crumbs[crumbs.index("profile") + 1] if "profile" in crumbs[:-1] else crumbs[-1]
    return text.strip().lower()


def _at(post):
    return ((post or {}).get("record") or {}).get("createdAt")


def _age(stamp):
    """Seconds since an ISO 8601 timestamp, or None if it cannot be read."""
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()))


def _who(author):
    return ((author or {}).get("displayName") or (author or {}).get("handle") or "someone")


def _flat(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:TEXT_MAX]


def _words(post):
    """A post as one line. Bluesky sends plain text, so this is mostly flattening it.

    A post can carry no words at all - a link on its own, or a picture on its own - and a
    block with a name and nothing under it reads as a failure. The card's headline and an
    image's alt text are what the post shows in that case.
    """
    said = _flat((post.get("record") or {}).get("text"))
    if said:
        return said
    embed = post.get("embed") or {}
    if (embed.get("$type") or "").startswith("app.bsky.embed.recordWithMedia"):
        embed = embed.get("media") or {}
    headline = (embed.get("external") or {}).get("title")
    images = embed.get("images") or ()
    return _flat(headline or (images[0].get("alt") if images else ""))


def _thumbnail(embed):
    """The URL of the one picture worth showing for a post, or None.

    A post carries at most one embed and four shapes of it matter: images, a quoted post with
    images beside it, and a link card, whose picture is the one the post actually shows. A
    video's thumbnail is a still of something moving and says less than the words do.
    """
    embed = embed or {}
    kind = embed.get("$type") or ""
    if kind.startswith("app.bsky.embed.recordWithMedia"):
        return _thumbnail(embed.get("media"))
    if kind.startswith("app.bsky.embed.images"):
        images = embed.get("images") or ()
        return images[0].get("thumb") if images else None
    if kind.startswith("app.bsky.embed.external"):
        return (embed.get("external") or {}).get("thumb")
    return None


def _post_item(entry):
    """One feed entry as the four things a notifications page draws.

    A feed hands back a post inside a wrapper that says why it is there, and a reply is handed
    back bare - so both are taken. A repost has no words of its own, and what is drawn is the
    post itself with who sent it round as the note.
    """
    post = entry.get("post") or entry
    reason = (entry.get("reason") or {}).get("$type") or ""
    note = None
    if reason.endswith("reasonRepost"):
        note = f"reposted by {_who((entry.get('reason') or {}).get('by'))}"
    elif reason.endswith("reasonPin"):
        note = "pinned"
    elif (post.get("record") or {}).get("reply"):
        note = "reply"
    return {
        "title": _who(post.get("author")),
        "text": _words(post),
        "age_s": _age(_at(post)),
        "note": note,
    }
