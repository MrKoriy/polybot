"""ESPN sports data source — free, no API key, team stats + injuries + scores.

Provides rich context for sports market analysis: recent form, head-to-head,
injuries, standings. This gives the LLM actual data to disagree with market prices
instead of just anchoring on them.
"""
import re
from datetime import datetime, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Map Polymarket question keywords to ESPN sport/league
SPORT_ROUTES = {
    # NBA
    "nba": "basketball/nba", "lakers": "basketball/nba", "celtics": "basketball/nba",
    "warriors": "basketball/nba", "bucks": "basketball/nba", "nuggets": "basketball/nba",
    "76ers": "basketball/nba", "cavaliers": "basketball/nba", "thunder": "basketball/nba",
    "timberwolves": "basketball/nba", "mavericks": "basketball/nba", "knicks": "basketball/nba",
    "heat": "basketball/nba", "magic": "basketball/nba", "pacers": "basketball/nba",
    "spurs": "basketball/nba", "hawks": "basketball/nba", "nets": "basketball/nba",
    "suns": "basketball/nba", "raptors": "basketball/nba", "jazz": "basketball/nba",
    "kings": "basketball/nba", "rockets": "basketball/nba", "clippers": "basketball/nba",
    "grizzlies": "basketball/nba", "pelicans": "basketball/nba", "blazers": "basketball/nba",
    "hornets": "basketball/nba", "pistons": "basketball/nba", "bulls": "basketball/nba",
    "wizards": "basketball/nba",
    # NHL
    "nhl": "hockey/nhl", "rangers": "hockey/nhl", "bruins": "hockey/nhl",
    "penguins": "hockey/nhl", "hurricanes": "hockey/nhl", "kraken": "hockey/nhl",
    "lightning": "hockey/nhl", "senators": "hockey/nhl", "sabres": "hockey/nhl",
    "blackhawks": "hockey/nhl", "red wings": "hockey/nhl", "blue jackets": "hockey/nhl",
    # Soccer (Premier League)
    "premier league": "soccer/eng.1", "liverpool": "soccer/eng.1",
    "arsenal": "soccer/eng.1", "manchester": "soccer/eng.1",
    "chelsea": "soccer/eng.1", "tottenham": "soccer/eng.1",
    "aston villa": "soccer/eng.1", "newcastle": "soccer/eng.1",
    # Soccer (La Liga)
    "la liga": "soccer/esp.1", "barcelona": "soccer/esp.1",
    "real madrid": "soccer/esp.1", "atletico": "soccer/esp.1",
    # Soccer (Serie A)
    "serie a": "soccer/ita.1", "internazionale": "soccer/ita.1",
    "milan": "soccer/ita.1", "juventus": "soccer/ita.1", "napoli": "soccer/ita.1",
    # NFL
    "nfl": "football/nfl", "chiefs": "football/nfl", "eagles": "football/nfl",
    # NCAA Basketball
    "ncaa": "basketball/mens-college-basketball",
    "hawkeyes": "basketball/mens-college-basketball",
    "gators": "basketball/mens-college-basketball",
    "longhorns": "basketball/mens-college-basketball",
    "bulldogs": "basketball/mens-college-basketball",
    "bruins": "basketball/mens-college-basketball",
    "huskies": "basketball/mens-college-basketball",
    "crimson tide": "basketball/mens-college-basketball",
    "wolverines": "basketball/mens-college-basketball",
    "spartans": "basketball/mens-college-basketball",
    "red raiders": "basketball/mens-college-basketball",
    "jayhawks": "basketball/mens-college-basketball",
    "volunteers": "basketball/mens-college-basketball",
    "cavaliers": "basketball/mens-college-basketball",
}


class ESPNSource(NewsSource):
    """ESPN sports data — scores, standings, team form, injuries."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )

    @property
    def name(self) -> str:
        return "espn_sports"

    async def health_check(self) -> bool:
        try:
            resp = await self._http.get(f"{ESPN_BASE}/basketball/nba/scoreboard")
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        """Fetch sports context for a market question.

        Instead of news articles, returns structured sports data as NewsItems
        that the LLM can use for analysis.
        """
        sport_route = self._detect_sport(query)
        if not sport_route:
            return []

        items: list[NewsItem] = []
        now = datetime.now(timezone.utc)

        try:
            # 1. Get today's scoreboard (live scores, upcoming games)
            scoreboard_items = await self._fetch_scoreboard(sport_route, query)
            items.extend(scoreboard_items)

            # 2. Get team-specific data if we can identify teams
            teams = self._extract_teams(query)
            for team in teams[:2]:  # Max 2 teams
                team_items = await self._fetch_team_data(sport_route, team)
                items.extend(team_items)

        except Exception:
            log.debug("espn_fetch_error", query=query[:50])

        if items:
            log.info("espn_fetched", query=query[:50], items=len(items))

        return items

    async def _fetch_scoreboard(self, sport_route: str, query: str) -> list[NewsItem]:
        """Get today's games with scores and status."""
        items = []
        try:
            resp = await self._http.get(f"{ESPN_BASE}/{sport_route}/scoreboard")
            if resp.status_code != 200:
                return []

            data = resp.json()
            now = datetime.now(timezone.utc)
            query_lower = query.lower()

            for event in data.get("events", []):
                name = event.get("name", "")
                # Only include games relevant to the query
                if not any(w in name.lower() for w in query_lower.split() if len(w) > 3):
                    continue

                status = event.get("status", {}).get("type", {}).get("shortDetail", "")
                comps = event.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])

                # Build rich context string
                parts = [f"GAME: {name} | Status: {status}"]

                for team in competitors:
                    t = team.get("team", {})
                    record = team.get("records", [{}])[0].get("summary", "") if team.get("records") else ""
                    score = team.get("score", "")
                    home_away = "HOME" if team.get("homeAway") == "home" else "AWAY"
                    parts.append(
                        f"  {t.get('displayName', '')} ({t.get('abbreviation', '')}) "
                        f"| {home_away} | Record: {record} | Score: {score}"
                    )

                # Odds if available
                odds = comps.get("odds", [{}])
                if odds:
                    o = odds[0]
                    spread = o.get("details", "")
                    overunder = o.get("overUnder", "")
                    if spread:
                        parts.append(f"  Spread: {spread} | O/U: {overunder}")

                # Headlines
                headlines = comps.get("headlines", [])
                for h in headlines[:2]:
                    parts.append(f"  Headline: {h.get('shortLinkText', '')}")

                body = "\n".join(parts)
                items.append(NewsItem(
                    title=f"ESPN: {name} — {status}",
                    body=body,
                    source="espn_sports/scoreboard",
                    url=event.get("links", [{}])[0].get("href", "") if event.get("links") else "",
                    published_at=now,
                ))

        except Exception:
            log.debug("espn_scoreboard_error", route=sport_route)

        return items

    async def _fetch_team_data(self, sport_route: str, team_name: str) -> list[NewsItem]:
        """Get team standings, recent form, and roster info."""
        items = []
        now = datetime.now(timezone.utc)

        try:
            # Search for team
            resp = await self._http.get(
                f"{ESPN_BASE}/{sport_route}/teams",
                params={"limit": 50},
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])

            # Find matching team
            team_lower = team_name.lower()
            matched = None
            for t in teams:
                t_data = t.get("team", {})
                names = [
                    t_data.get("displayName", "").lower(),
                    t_data.get("shortDisplayName", "").lower(),
                    t_data.get("abbreviation", "").lower(),
                    t_data.get("nickname", "").lower(),
                ]
                if any(team_lower in n or n in team_lower for n in names if n):
                    matched = t_data
                    break

            if not matched:
                return []

            team_id = matched.get("id", "")
            team_display = matched.get("displayName", team_name)

            # Get team record and standing
            record = matched.get("record", {}).get("items", [])
            record_str = ""
            if record:
                stats = record[0].get("stats", [])
                for s in stats:
                    if s.get("name") == "overall":
                        record_str = s.get("displayValue", "")
                    elif s.get("name") in ("wins", "losses"):
                        record_str += f" {s.get('name')}={s.get('value', '')}"

            standingSummary = matched.get("standingSummary", "")

            # Build context
            body = (
                f"TEAM PROFILE: {team_display}\n"
                f"  Record: {record_str}\n"
                f"  Standing: {standingSummary}\n"
            )

            # Try to get recent results
            try:
                results_resp = await self._http.get(
                    f"{ESPN_BASE}/{sport_route}/teams/{team_id}/schedule"
                )
                if results_resp.status_code == 200:
                    schedule = results_resp.json()
                    events = schedule.get("events", [])
                    # Last 5 completed games
                    recent = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed", False)]
                    recent = recent[-5:] if len(recent) > 5 else recent

                    if recent:
                        body += "  LAST 5 GAMES:\n"
                        for game in recent:
                            comp = game.get("competitions", [{}])[0]
                            competitors = comp.get("competitors", [])
                            opp = ""
                            result = ""
                            for c in competitors:
                                if str(c.get("id", "")) == str(team_id):
                                    winner = c.get("winner", False)
                                    score = c.get("score", {}).get("displayValue", c.get("score", ""))
                                    result = "W" if winner else "L"
                                else:
                                    opp = c.get("team", {}).get("abbreviation", "")
                                    opp_score = c.get("score", {}).get("displayValue", c.get("score", ""))
                            body += f"    {result} vs {opp} ({score}-{opp_score})\n"
            except Exception:
                pass

            items.append(NewsItem(
                title=f"ESPN: {team_display} profile & recent form",
                body=body[:600],
                source="espn_sports/team",
                url="",
                published_at=now,
            ))

        except Exception:
            log.debug("espn_team_error", team=team_name)

        return items

    def _detect_sport(self, query: str) -> str | None:
        """Detect which ESPN sport route matches the query."""
        q = query.lower()
        for keyword, route in SPORT_ROUTES.items():
            if keyword in q:
                return route
        # Check for generic "vs" pattern (likely sports)
        if " vs" in q or " vs." in q:
            return "basketball/nba"  # default to NBA as most common
        return None

    def _extract_teams(self, query: str) -> list[str]:
        """Extract team names from a market question."""
        # Pattern: "Team A vs. Team B" or "Team A at Team B"
        q = query.strip()
        # Remove common prefixes
        for prefix in ["Spread:", "Will", "O/U"]:
            if q.startswith(prefix):
                q = q[len(prefix):].strip()

        # Split on "vs" or "at"
        parts = re.split(r'\s+(?:vs\.?|at|@)\s+', q, maxsplit=1)
        teams = []
        for p in parts:
            # Clean up: remove scores, spreads, dates
            clean = re.sub(r'\(.*?\)', '', p).strip()
            clean = re.sub(r'\s*[-:]\s*\d+\.?\d*$', '', clean).strip()
            clean = re.sub(r'\s+on\s+\d{4}-\d{2}-\d{2}.*', '', clean).strip()
            clean = re.sub(r'\s+FC$', '', clean).strip()
            if clean and len(clean) > 2:
                teams.append(clean)
        return teams

    async def close(self) -> None:
        await self._http.aclose()
