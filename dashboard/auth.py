"""
dashboard/auth.py — Discord OAuth2 helpers for Friez
     Welcome to Chezburger's Community! (https://discord.gg/9bm7uXVc2w)
"""

import os, secrets, requests
from flask import session

CLIENT_ID     = os.getenv('DISCORD_CLIENT_ID')
CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
REDIRECT_URI  = os.getenv('DISCORD_REDIRECT_URI', 'http://localhost:5000/friez/dashboard/callback')

DISCORD_API   = 'https://discord.com/api/v10'
SCOPES        = 'identify guilds.members.read'


def get_oauth_url() -> str:
    """Build the OAuth2 URL — first step to getting a seat at the counter."""
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = (
        f'?client_id={CLIENT_ID}'
        f'&redirect_uri={requests.utils.quote(REDIRECT_URI)}'
        f'&response_type=code'
        f'&scope={requests.utils.quote(SCOPES)}'
        f'&state={state}'
    )
    return f'https://discord.com/oauth2/authorize{params}'


def verify_state(state: str) -> bool:
    """Make sure the order ticket matches — no ticket, no burger."""
    return state and state == session.pop('oauth_state', None)


def exchange_code(code: str) -> dict | None:
    """Trade in the code for an access token. Order up!"""
    r = requests.post(f'{DISCORD_API}/oauth2/token', data={
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type':    'authorization_code',
        'code':          code,
        'redirect_uri':  REDIRECT_URI,
    }, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    return r.json() if r.ok else None


def get_user_info(access_token: str) -> dict:
    """Fetch the customer's info — who's at the window?"""
    r = requests.get(f'{DISCORD_API}/users/@me',
                     headers={'Authorization': f'Bearer {access_token}'})
    return r.json() if r.ok else {}


def get_guild_member(access_token: str, guild_id: int) -> dict | None:
    """Check if this customer is a regular at Chezburger's Community."""
    r = requests.get(f'{DISCORD_API}/users/@me/guilds/{guild_id}/member',
                     headers={'Authorization': f'Bearer {access_token}'})
    return r.json() if r.ok else None


# Guild permissions integer needs the guild's role objects to compute properly.
# We use a simplified version: just check the raw permissions field on the member.
# Discord sends `permissions` as a string on the member object when fetched via OAuth.
def compute_permissions(member_info: dict, guild_id: int) -> int:
    """Return raw permissions integer — how much can this customer order?"""
    perms = member_info.get('permissions')
    if perms is not None:
        return int(perms)
    # Fallback: fetch guild roles and compute (needs bot token)
    return 0
