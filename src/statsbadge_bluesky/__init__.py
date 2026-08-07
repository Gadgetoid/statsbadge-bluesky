"""Bluesky accounts and feeds, for a badge to show on a notifications page.

Everything comes from the public AppView at `https://public.api.bsky.app/xrpc`, which serves
these unauthenticated. No account, no app password, nothing to keep secret - the cost being
that only what anyone could see is here.

    com.atproto.identity.resolveHandle    a handle to the DID a feed URI is built from
    app.bsky.actor.getProfile             the counters, and who this is
    app.bsky.feed.getAuthorFeed           the newest post, and its numbers
    app.bsky.feed.getPostThread           the newest reply to it, which is the public
                                          half of a mention: notifications need a login
    app.bsky.feed.getFeed                 the newest post in a feed
    app.bsky.feed.getFeedGenerator        that feed's name and how many like it

Every handle and every feed becomes a separate group, so the config UI offers them by name
under one Bluesky heading and a page can take a message from one and a counter from another.

The messages travel in the shape a `notify` page draws: who sent it, the words, how long ago,
and a note about why. A Mastodon post and an RSS entry arrive in the same shape, so nothing is
Bluesky-specific by the time it reaches the badge.
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

# How often the AppView is asked, unless the `every` setting overrides it. A timeline is not
# a sensor, and a handful of requests every two minutes is nothing to a public service.
DEFAULT_EVERY = 120.0
MIN_EVERY = 30.0
MAX_EVERY = 3600.0
RETRY_AFTER = 60.0
FETCH_POLL = 1.0

# How far back to read an author's feed. Enough to find one post of theirs that somebody has
# replied to, on an account that reposts a good deal.
FEED_SCAN = 25

# How many of those threads to open looking for a reply from somebody else. More than one,
# since the only reply is often the author carrying on their own thought; few, since each
# costs a request per handle watched.
REPLY_SCAN = 3

# A post is three hundred characters and the page draws two or three lines of it. Cut here
# rather than on the badge: the rest is neither drawn nor worth sending every time it changes.
TEXT_MAX = 160

# The counters, kept once an hour so a graph of them shows a trend. The AppView reports no
# history, so this is the only place one can come from: a ring starts empty and fills as the
# host runs.
HISTORY_EVERY = 3600.0
HISTORY_POINTS = 48
HISTORY_MS = int(HISTORY_EVERY * 1000)
HISTORIED = ("followers", "following", "posts")
COUNTS = "counts"
NAMES = "names"

# Shown when no handles or feeds are configured. Not counted as a fault, but it has to be
# visible: a source quietly reporting nothing looks the same as one that was never installed.
UNSET = "no handles or feeds set"

# The imaging preset each `images` choice maps to. Landscape either way, since a page sets
# the picture beside two or three lines of text.
PRESETS = {"small": "low", "large": "high"}
# How many decoded pictures to keep, keyed by the post they came from: the same few posts are
# refetched every couple of minutes and their images have not changed.
IMAGE_CACHE = 24

# What a group is called in the frame. A field reference is "group.field" split on its one
# dot, so neither a handle nor a feed can keep its punctuation.
ACCOUNT_PREFIX = "bsky_"
FEED_PREFIX = "bskyfeed_"

FIELDS = {
    "latest": {"label": "Latest post", "item": True},
    "reply": {"label": "Latest reply", "item": True},
    "followers": {"label": "Followers", "history": True},
    "following": {"label": "Following", "history": True},
    "posts": {"label": "Posts", "history": True},
    "likes": {"label": "Likes on the latest"},
    "reposts": {"label": "Reposts of the latest"},
    "replies": {"label": "Replies to the latest"},
    "quotes": {"label": "Quotes of the latest"},
}

FEED_FIELDS = {
    "latest": {"label": "Latest post", "item": True},
    "likes": {"label": "Likes on the feed"},
}


class Bluesky(Source):
    name = "bluesky"
    label = "Bluesky"

    settings = (
        {"key": "handles", "label": "Handles", "type": "text",
         "hint": "Accounts to watch, separated by commas: gadgetoid.com, "
                 "someone.bsky.social. The @, and a profile URL pasted whole, both work"},
        {"key": "feeds", "label": "Feeds", "type": "text",
         "hint": "Feeds to watch, separated by commas. A feed's page on bsky.app, or the "
                 "at:// URI it is published under"},
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
        # What each group turned out to be called, and each handle's DID. Both are learned
        # from a fetch and both outlive it: a feed keeps the display name it answered with,
        # and a DID resolved once is a request not made again.
        self._names = {}
        self._dids = {}
        self._lock = threading.Lock()
        self._next = 0.0
        self._next_history = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Take up what the last run kept, then fetch on a background thread.

        Nothing in `sample` may wait on a network: every source shares the collector's
        thread and the first sample is taken while the server is still starting up.
        """
        kept = self.store.get(COUNTS) or {}
        with self._lock:
            self._counts = {name: list(points) for name, points in kept.items()
                            if isinstance(points, list)}
            # Names too, so a feed carries its display name on the first page load, before
            # any fetch has landed.
            self._names = dict(self.store.get(NAMES) or {})
        self._read_settings()
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
        """Take settings while running, and ask again without waiting out the interval."""
        super().configure(settings)
        was = {entry["slug"] for entry in self._watched}
        self._read_settings()
        if self.last_fault == UNSET and self._watched:
            # That message was about the settings, and they have just been given. Waiting for
            # a fetch to succeed before withdrawing it leaves the config page saying nothing
            # is set for as long as the first few requests take.
            self.last_fault = None
        if was != {entry["slug"] for entry in self._watched}:
            with self._lock:
                self._readings = {group: values for group, values in self._readings.items()
                                  if group in self.groups}
        self._next = 0.0
        self._wake.set()

    # -- what this source offers --------------------------------------------

    def _read_settings(self):
        """Re-read the settings, and rebuild what is offered from them.

        `groups` is read off the source, not the class, so a handle typed into the config UI
        is a group the page picker offers as soon as it is saved. The readings follow when
        the first fetch lands.
        """
        try:
            every = float(self.config.get("every") or DEFAULT_EVERY)
        except (TypeError, ValueError):
            every = DEFAULT_EVERY
        self.every = max(MIN_EVERY, min(MAX_EVERY, every))
        wanted = str(self.config.get("images") or "small")
        # Off where the extra is not installed, rather than a fault on every fetch: on a host
        # with no decoder the words alone are the message.
        self.preset = PRESETS.get(wanted)

        watched = []
        taken = set()
        for handle in _listed(self.config.get("handles"), _handle):
            slug = _unique(ACCOUNT_PREFIX + _slug(handle), taken)
            watched.append({"slug": slug, "kind": "account", "spec": handle,
                            "label": handle})
        for feed in _listed(self.config.get("feeds"), str.strip):
            slug = _unique(FEED_PREFIX + _slug(_rkey(feed)), taken)
            watched.append({"slug": slug, "kind": "feed", "spec": feed,
                            "label": _rkey(feed)})
        with self._lock:
            names = dict(self._names)
        self._watched = watched
        # Slow, every one of them: a timeline is fetched every couple of minutes and the badge
        # polls every second, so they travel when they change and not a hundred times over.
        self.groups = {
            entry["slug"]: {
                "label": names.get(entry["slug"]) or entry["label"],
                "slow": True,
                "fields": dict(FIELDS if entry["kind"] == "account" else FEED_FIELDS),
            }
            for entry in watched
        }
        self.provides = tuple(self.groups)

    def _learn_name(self, slug, name):
        """Remember what a group turned out to be called, and offer it under that.

        A feed is configured as a URI and called something else - `mechkeebs` is "Mechanical
        Keyboards" - and the picker should show the second. Kept, so the name survives a
        restart and is there on the first page load after one.
        """
        if not name:
            return
        with self._lock:
            if self._names.get(slug) == name:
                return
            self._names[slug] = name
            kept = dict(self._names)
        self.store.set(NAMES, kept)
        self._read_settings()

    # -- sampling -----------------------------------------------------------

    def sample(self, frame, dt):
        """Whatever the fetcher last brought back, copied out under the lock."""
        with self._lock:
            readings = {group: dict(values) for group, values in self._readings.items()
                        # A handle taken out of the settings a moment ago is still in the last
                        # answer, and a group nothing declares is a group nothing can draw.
                        if group in self.groups}
        for group, values in readings.items():
            frame[group] = values

    def series(self):
        """The counter rings, on the hour they are kept at.

        The collector would sample these at its own rate, and ninety seconds of a follower
        count is a flat line. An hour apart is the shape of a week.
        """
        with self._lock:
            counts = {name: list(points) for name, points in self._counts.items()
                      if name.split(".")[0] in self.groups}
            at = self._counts_at
        if not counts or at is None:
            return {}
        age_ms = max(0, int((time.monotonic() - at) * 1000))
        return {ref: {"points": points, "every_ms": HISTORY_MS, "age_ms": age_ms}
                for ref, points in counts.items() if points}

    def note_fault(self, exc):
        """Record an AppView error as plain text, without a type name in front of it."""
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
                # The fetcher has to survive a bad fetch, or the timeline would stand at
                # whatever it last was for as long as the host runs.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _refresh(self):
        if not self._watched:
            # Not a fault: an extension nobody has given an account to is unconfigured, and
            # counting that would report a broken source on every host that installed it.
            self.last_fault = UNSET
            return
        if time.monotonic() < self._next:
            return
        readings = {}
        trouble = None
        for entry in list(self._watched):
            try:
                if entry["kind"] == "account":
                    readings[entry["slug"]] = self._fetch_account(entry)
                else:
                    readings[entry["slug"]] = self._fetch_feed(entry)
            except Exception as exc:
                # One handle spelled wrong must not cost the others their readings, so what
                # went wrong is carried and reported once everything else has been tried.
                trouble = exc
        self._next = time.monotonic() + (self.every if readings else RETRY_AFTER)
        if readings:
            with self._lock:
                self._readings.update(readings)
            self._keep_counts(readings)
            self.note_ok()
        if trouble is not None:
            # After note_ok, which clears a fault: whatever did answer stands, and a handle
            # typed wrong is still wrong.
            self.note_fault(trouble)

    def _fetch_account(self, entry):
        handle = entry["spec"]
        profile = self._get("app.bsky.actor.getProfile", actor=handle)
        readings = {
            "followers": profile.get("followersCount"),
            "following": profile.get("followsCount"),
            "posts": profile.get("postsCount"),
        }
        did = profile.get("did")
        self._dids[handle] = did

        feed = self._get("app.bsky.feed.getAuthorFeed", actor=handle,
                         limit=FEED_SCAN, filter="posts_no_replies").get("feed") or ()
        # Their own newest, reposts excluded: a repost carries somebody else's numbers, and
        # "likes on the latest" would be a stranger's.
        mine = next((item for item in feed if not item.get("reason")), None)
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
        return readings

    def _newest_reply(self, feed, did):
        """The newest reply somebody else left on one of their recent posts.

        The public AppView serves no notifications, and post search needs a login, so the
        threads under their posts are where a mention has to come from. A reply from the
        account itself is skipped: a thread they are carrying on alone is not an answer.
        """
        answered = [item for item in feed
                    if not item.get("reason") and (item["post"].get("replyCount") or 0)]
        for item in answered[:REPLY_SCAN]:
            thread = self._get("app.bsky.feed.getPostThread",
                               uri=item["post"]["uri"], depth=1).get("thread") or {}
            replies = [reply for reply in (thread.get("replies") or ())
                       if reply.get("post")
                       and reply["post"].get("author", {}).get("did") != did]
            if replies:
                return max(replies, key=lambda reply: _at(reply["post"]) or "")
        return None

    def _fetch_feed(self, entry):
        uri = self._feed_uri(entry["spec"])
        posts = self._get("app.bsky.feed.getFeed", feed=uri, limit=1).get("feed") or ()
        about = self._get("app.bsky.feed.getFeedGenerator", feed=uri).get("view") or {}
        self._learn_name(entry["slug"], about.get("displayName"))
        readings = {"likes": about.get("likeCount")}
        if posts:
            readings["latest"] = self._with_picture(_post_item(posts[0]),
                                                    posts[0].get("post"))
        return readings

    def _feed_uri(self, spec):
        """The at:// URI of a configured feed, from either way of writing one.

        A feed is shared as its page on bsky.app - `/profile/<handle>/feed/<name>` - and
        published as `at://<did>/app.bsky.feed.generator/<name>`. Pasting the first is what
        anybody will do, and the handle in it has to be resolved: an at:// URI names a DID.
        """
        if spec.startswith("at://"):
            return spec
        parts = urllib.parse.urlsplit(spec if "//" in spec else f"//{spec}")
        crumbs = [crumb for crumb in parts.path.split("/") if crumb]
        if len(crumbs) >= 4 and crumbs[0] == "profile" and crumbs[2] == "feed":
            who, name = _handle(crumbs[1]), crumbs[3]
            did = self._dids.get(who)
            if not did:
                did = self._get("com.atproto.identity.resolveHandle",
                                handle=who).get("did")
                self._dids[who] = did
            return f"at://{did}/app.bsky.feed.generator/{name}"
        raise BlueskyError(f"cannot tell what feed {spec!r} is")

    def _with_picture(self, item, post):
        """`item` with one picture on it, where the post has one and `images` is on.

        One per post: a page has room for one, and the first image is the one the author led
        with. A picture that will not fetch or will not decode is omitted: the words are the
        post, and a message with no picture is a smaller message, not a failure worth
        reporting.
        """
        if item is None or not self.preset or not post:
            return item
        url = _thumbnail(post.get("embed"))
        if not url:
            return item
        # Keyed on the preset too, so changing the setting redraws at the new size on the
        # next fetch.
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
            for group, values in readings.items():
                for name in HISTORIED:
                    value = values.get(name)
                    if value is None:
                        continue
                    ring = self._counts.setdefault(f"{group}.{name}", [])
                    ring.append(int(value))
                    del ring[0:max(0, len(ring) - HISTORY_POINTS)]
            self._counts_at = now
            kept = {ref: list(points) for ref, points in self._counts.items()}
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
            # The status alone is not enough: a handle that does not exist and a handle
            # prefixed with an @ are both 400. The body is where the difference is.
            detail = ""
            try:
                said = json.loads(exc.read().decode("utf-8")) or {}
                detail = said.get("message") or said.get("error") or ""
            except Exception:
                detail = ""
            raise BlueskyError(f"HTTP {exc.code}"
                               + (f": {detail}" if detail else "")) from exc


class BlueskyError(Exception):
    """An AppView error, as one line for the config UI to show."""


# -- reading the settings ---------------------------------------------------

def _listed(given, clean):
    """A comma or newline separated setting as a list, in order, without repeats."""
    out = []
    for part in re.split(r"[,\n]", str(given or "")):
        entry = clean(part)
        if entry and entry not in out:
            out.append(entry)
    return out


def _handle(given):
    """A handle in the form the AppView accepts: no @, no https://, no trailing path.

    People paste their profile URL, and people type the @ they see on the page. Both are a
    400 from an endpoint expecting `gadgetoid.com`.
    """
    text = str(given or "").strip().lstrip("@")
    if "/" in text:
        text = text.replace("https://", "").replace("http://", "")
        crumbs = [crumb for crumb in text.split("/") if crumb]
        # bsky.app/profile/<handle>, and anything else keeps its last part.
        text = crumbs[crumbs.index("profile") + 1] if "profile" in crumbs[:-1] else crumbs[-1]
    return text.strip().lower()


def _rkey(spec):
    """What a feed is called in its URI, which names it until the AppView is asked.

    Both ways of writing one end in it: `.../feed/mechkeebs` and
    `at://<did>/app.bsky.feed.generator/mechkeebs`.
    """
    crumbs = [crumb for crumb in str(spec or "").split("/") if crumb]
    return crumbs[-1] if crumbs else "feed"


def _slug(name):
    """A handle or a feed as a group name: "pinout.xyz" is `bsky_pinout_xyz`."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "one"


def _unique(slug, taken):
    """`slug`, numbered if something else already has it.

    Two feeds can be called the same thing by two people, and a group is one set of readings:
    the second would otherwise overwrite the first and only one of them would be offered.
    """
    candidate, count = slug, 1
    while candidate in taken:
        count += 1
        candidate = f"{slug}_{count}"
    taken.add(candidate)
    return candidate


# -- turning a post into a message ------------------------------------------

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
    return (author or {}).get("displayName") or (author or {}).get("handle") or "someone"


def _flat(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:TEXT_MAX]


def _words(post):
    """A post as one line. Bluesky sends plain text, so this is mostly flattening it.

    A post can carry no words at all - a bare link, or a bare picture - and a
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

    A post carries at most one embed and three shapes of it matter: images, a quoted post with
    images beside it, and a link card, whose picture is the one the post actually shows. A
    video's thumbnail is a still of something moving and shows less than the words do.
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

    A feed hands back a post inside a wrapper giving the reason it is there, and a reply is
    handed back bare - so both are taken. A repost has no words of its own, and what is drawn
    is the post itself with who sent it round as the note.
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
