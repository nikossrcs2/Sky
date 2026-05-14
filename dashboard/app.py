"""
dashboard/app.py — Friez web dashboard
Runs on Pi5, accessed via Tor hidden service at chezburgers.onion

Routes:
  /                           → server landing page
  /credits/                   → credits page
  /friez/                     → friez info
  /friez/dashboard/           → dashboard hub (OAuth gate)
  /friez/dashboard/callback   → Discord OAuth callback
  /friez/dashboard/<section>/ → individual setting sections
"""

import os, sys, json, secrets
from functools import wraps
from flask import (Flask, render_template, redirect, url_for,
                   request, session, flash, jsonify, g)
from dotenv import load_dotenv

# Load .env from parent dir (same place Friez.py lives)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import db

import auth

app = Flask(__name__)
app.secret_key = os.getenv('DASHBOARD_SECRET_KEY') or (_ for _ in ()).throw(
    RuntimeError("DASHBOARD_SECRET_KEY env var not set — sessions would reset on every restart.")
)

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(429)
def too_many_requests(e):
    return render_template('429.html'), 429

@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500

@app.errorhandler(503)
def service_unavailable(e):
    return render_template('503.html'), 503

@app.errorhandler(418)
def im_a_teapot(e):
    return render_template('418.html'), 418

@app.errorhandler(400)
def bad_request(e):
    return render_template('error.html', code=400, description=e.description), 400

@app.errorhandler(Exception)
def generic_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return render_template('error.html', code=e.code, description=e.description), e.code
    return render_template('error.html', code=500, description='An unexpected error occurred.'), 500

GUILD_ID      = int(os.getenv('GUILD_ID', '1414998098526212257'))
MOD_ROLE_ID   = 1492116260568174813
DEV_ID        = 1458255715796910315
OWNER_ID      = 1409653725051752458


@app.before_request
def _before_request():
    import random
    if random.random() < 0.01:
        db.purge_expired_sessions()



# ── Permission helpers ────────────────────────────────────────────────────────

def _get_session_user():
    """Return (user_dict, tier) or (None, None). tier: 'member'|'mod'|'admin'|'dev'"""
    sid = session.get('session_id')
    if not sid:
        return None, None
    row = db.get_session(sid)
    if not row:
        session.pop('session_id', None)
        return None, None
    user = json.loads(row['user_id']) if isinstance(row['user_id'], str) and row['user_id'].startswith('{') else None
    # User info stored separately
    uinfo = session.get('user_info', {})
    uid   = int(uinfo.get('id', 0))
    roles = session.get('guild_roles', [])  # list of role IDs (ints)
    perms = session.get('guild_permissions', 0)

    if uid == DEV_ID:
        tier = 'dev'
    elif (perms & 0x8):  # administrator flag
        tier = 'admin'
    elif MOD_ROLE_ID in roles:
        tier = 'mod'
    else:
        tier = 'member'
    return uinfo, tier


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uinfo, tier = _get_session_user()
        if not uinfo:
            session['next'] = request.url
            return redirect(url_for('dashboard_login'))
        g.user = uinfo
        g.tier = tier
        return f(*args, **kwargs)
    return decorated


def requires_tier(minimum):
    """minimum: 'member' < 'mod' < 'admin' < 'dev'"""
    ORDER = {'member': 0, 'mod': 1, 'admin': 2, 'dev': 3}
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            if ORDER.get(g.tier, 0) < ORDER[minimum]:
                flash('You do not have permission to access this section.', 'error')
                return redirect(url_for('dashboard_home'))
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.route('/friez/api/guild_cache')
@login_required
def api_guild_cache():
    cache = db.get_global_config('guild_cache', {})
    return jsonify(cache)


# ── Public pages ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/credits/')
def credits():
    return render_template('credits.html')

@app.route('/friez/')
def friez():
    return render_template('friez.html')


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/friez/dashboard/login')
def dashboard_login():
    uinfo, _ = _get_session_user()
    if uinfo:
        return redirect(url_for('dashboard_home'))
    oauth_url = auth.get_oauth_url()
    return render_template('login.html', oauth_url=oauth_url)

@app.route('/friez/dashboard/callback')
def dashboard_callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    if not code or not auth.verify_state(state):
        flash('OAuth failed — invalid state. Try again.', 'error')
        return redirect(url_for('dashboard_login'))

    token_data  = auth.exchange_code(code)
    if not token_data:
        flash('Failed to get Discord token.', 'error')
        return redirect(url_for('dashboard_login'))

    user_info   = auth.get_user_info(token_data['access_token'])
    member_info = auth.get_guild_member(token_data['access_token'], GUILD_ID)

    if not member_info:
        flash("You must be a member of Chezburger's Community to access the dashboard.", 'error')
        return redirect(url_for('dashboard_login'))

    roles = [int(r) for r in member_info.get('roles', [])]
    perms = auth.compute_permissions(member_info, GUILD_ID)

    # Store session
    sid = secrets.token_urlsafe(32)
    import time
    db.create_session(sid, user_info['id'], token_data['access_token'],
                      time.time() + 7 * 86400)
    session['session_id']         = sid
    session['user_info']          = user_info
    session['guild_roles']        = roles
    session['guild_permissions']  = perms
    session.permanent             = True

    next_url = session.pop('next', url_for('dashboard_home'))
    return redirect(next_url)

@app.route('/friez/dashboard/logout')
def dashboard_logout():
    sid = session.pop('session_id', None)
    if sid:
        db.delete_session(sid)
    session.clear()
    return redirect(url_for('index'))


# ── Dashboard hub ─────────────────────────────────────────────────────────────

@app.route('/friez/dashboard/')
@login_required
def dashboard_home():
    return render_template('dashboard.html', user=g.user, tier=g.tier)


# ── Section: Economy ──────────────────────────────────────────────────────────

@app.route('/friez/dashboard/economy/')
@requires_tier('mod')
def section_economy():
    return render_template('sections/economy.html', user=g.user, tier=g.tier)

@app.route('/friez/dashboard/economy/api/user', methods=['GET'])
@requires_tier('mod')
def economy_get_user():
    uid = request.args.get('user_id', '').strip()
    if not uid:
        return jsonify({'error': 'No user_id'}), 400
    u   = db.get_user(uid)
    inv = db.get_inventory(uid)
    return jsonify({'user': u, 'inventory': inv})

@app.route('/friez/dashboard/economy/api/award', methods=['POST'])
@requires_tier('mod')
def economy_award():
    data     = request.json
    uid      = data.get('user_id')
    currency = data.get('currency')  # 'fc' (BB) or 'cr' (CC)
    amount   = int(data.get('amount', 0))
    if currency not in ('fc', 'cr') or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    if currency == 'fc': db.add_fc(uid, amount)
    else:                db.add_cr(uid, amount)
    return jsonify({'ok': True, 'new': db.get_user(uid)})

@app.route('/friez/dashboard/economy/api/deduct', methods=['POST'])
@requires_tier('mod')
def economy_deduct():
    data     = request.json
    uid      = data.get('user_id')
    currency = data.get('currency')
    amount   = int(data.get('amount', 0))
    if currency not in ('fc', 'cr') or amount <= 0:
        return jsonify({'error': 'Invalid'}), 400
    ok = db.deduct_fc(uid, amount) if currency == 'fc' else db.deduct_cr(uid, amount)
    return jsonify({'ok': ok, 'new': db.get_user(uid)})

@app.route('/friez/dashboard/economy/api/leaderboard')
@login_required
def economy_leaderboard():
    return jsonify(db.get_fc_leaderboard(20))


# ── Section: Moderation ───────────────────────────────────────────────────────

@app.route('/friez/dashboard/moderation/')
@requires_tier('mod')
def section_moderation():
    bans    = [b for b in db.get_all_ai_bans() if db.is_ai_banned(b['user_id'])]
    stmutes = db.get_all_stmutes()
    return render_template('sections/moderation.html', user=g.user, tier=g.tier,
                           bans=bans, stmutes=stmutes)

@app.route('/friez/dashboard/moderation/api/aiban', methods=['POST'])
@requires_tier('mod')
def mod_aiban():
    import time
    data   = request.json
    uid    = data.get('user_id')
    dur    = data.get('duration', 'permanent')
    reason = data.get('reason', 'Dashboard action')
    expiry = None if dur == 'permanent' else time.time() + _parse_dur(dur)
    db.set_ai_ban(uid, expiry=expiry, scanner_flagged=False, reason=reason)
    return jsonify({'ok': True})

@app.route('/friez/dashboard/moderation/api/aiunban', methods=['POST'])
@requires_tier('mod')
def mod_aiunban():
    data = request.json
    uid  = data.get('user_id')
    # Only dev can lift scanner bans
    if db.is_scanner_flagged(uid) and int(g.user.get('id', 0)) != DEV_ID:
        return jsonify({'error': 'Scanner ban — only dev can lift this.'}), 403
    db.lift_ai_ban(uid)
    return jsonify({'ok': True})

@app.route('/friez/dashboard/moderation/api/unstmute', methods=['POST'])
@requires_tier('mod')
def mod_unstmute():
    uid = request.json.get('user_id')
    db.remove_stmute(uid)
    return jsonify({'ok': True})


# ── Section: Word Filters / Automod ──────────────────────────────────────────

@app.route('/friez/dashboard/automod/')
@requires_tier('mod')
def section_automod():
    filters = db.get_word_filters(GUILD_ID)
    return render_template('sections/automod.html', user=g.user, tier=g.tier, filters=filters)

@app.route('/friez/dashboard/automod/api/add', methods=['POST'])
@requires_tier('mod')
def automod_add():
    data = request.json
    db.add_word_filter(GUILD_ID, data['list_name'], data['word'], data.get('action', 'delete'))
    return jsonify({'ok': True})

@app.route('/friez/dashboard/automod/api/remove', methods=['POST'])
@requires_tier('mod')
def automod_remove():
    data = request.json
    db.remove_word_filter(GUILD_ID, data['list_name'], data['word'])
    return jsonify({'ok': True})


# ── Section: AI Config ────────────────────────────────────────────────────────

@app.route('/friez/dashboard/ai/')
@requires_tier('admin')
def section_ai():
    channel_id = db.get_setting(GUILD_ID, 'ai_channel')
    scan_model = os.getenv('DM_SCAN_MODEL', 'smollm2')
    return render_template('sections/ai.html', user=g.user, tier=g.tier,
                           channel_id=channel_id, scan_model=scan_model)

@app.route('/friez/dashboard/ai/api/setchannel', methods=['POST'])
@requires_tier('admin')
def ai_setchannel():
    cid = request.json.get('channel_id')
    db.set_setting(GUILD_ID, 'ai_channel', cid)
    return jsonify({'ok': True})

@app.route('/friez/dashboard/ai/api/unsetchannel', methods=['POST'])
@requires_tier('admin')
def ai_unsetchannel():
    db.delete_setting(GUILD_ID, 'ai_channel')
    return jsonify({'ok': True})


# ── Section: Roles (RR, RC, Join Roles) ──────────────────────────────────────

@app.route('/friez/dashboard/roles/')
@requires_tier('admin')
def section_roles():
    rr = db.get_reaction_roles(GUILD_ID)
    return render_template('sections/roles.html', user=g.user, tier=g.tier, reaction_roles=rr)

@app.route('/friez/dashboard/roles/api/rr/add', methods=['POST'])
@requires_tier('admin')
def roles_rr_add():
    data = request.json
    db.add_reaction_role(GUILD_ID, data['message_id'], data['emoji'], data['role_id'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/roles/api/rr/remove', methods=['POST'])
@requires_tier('admin')
def roles_rr_remove():
    data = request.json
    db.remove_reaction_role(data['message_id'], data['emoji'])
    return jsonify({'ok': True})


# ── Section: Counting ─────────────────────────────────────────────────────────

@app.route('/friez/dashboard/counting/')
@requires_tier('admin')
def section_counting():
    cdata = db.get_count_data(GUILD_ID)
    return render_template('sections/counting.html', user=g.user, tier=g.tier, cdata=cdata)

@app.route('/friez/dashboard/counting/api/setsafes', methods=['POST'])
@requires_tier('admin')
def counting_setsafes():
    n = int(request.json.get('safes', 0))
    db.set_safes(GUILD_ID, n)
    return jsonify({'ok': True})

@app.route('/friez/dashboard/counting/api/setcount', methods=['POST'])
@requires_tier('admin')
def counting_setcount():
    n = int(request.json.get('count', 0))
    db.update_count(GUILD_ID, n, None)
    return jsonify({'ok': True})


# ── Section: Achievements ────────────────────────────────────────────────────

@app.route('/friez/dashboard/achievements/')
@requires_tier('admin')
def section_achievements():
    defs = db.get_all_achievement_defs()
    return render_template('sections/achievements.html', user=g.user, tier=g.tier, defs=defs)

@app.route('/friez/dashboard/achievements/api/upsert', methods=['POST'])
@requires_tier('admin')
def achievements_upsert():
    d = request.json
    db.upsert_achievement_def(d['id'], d['name'], d['description'], d['condition'], d.get('color', '#5865F2'))
    return jsonify({'ok': True})

@app.route('/friez/dashboard/achievements/api/delete', methods=['POST'])
@requires_tier('admin')
def achievements_delete():
    db.delete_achievement_def(request.json['id'])
    return jsonify({'ok': True})


# ── Section: Callsigns ────────────────────────────────────────────────────────

@app.route('/friez/dashboard/callsigns/')
@requires_tier('mod')
def section_callsigns():
    all_cs = db.get_all_callsigns()
    return render_template('sections/callsigns.html', user=g.user, tier=g.tier, callsigns=all_cs)

@app.route('/friez/dashboard/callsigns/api/set', methods=['POST'])
@requires_tier('admin')
def callsigns_set():
    data = request.json
    db.set_callsign(data['user_id'], data['callsign'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/callsigns/api/delete', methods=['POST'])
@requires_tier('admin')
def callsigns_delete():
    db.delete_callsign(request.json['user_id'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/settings/')
@requires_tier('admin')
def section_settings():
    all_settings = db.get_all_settings(GUILD_ID)
    return render_template('sections/settings.html', user=g.user, tier=g.tier,
                           settings=all_settings)

@app.route('/friez/dashboard/settings/api/set', methods=['POST'])
@requires_tier('admin')
def settings_set():
    data = request.json
    db.set_setting(GUILD_ID, data['key'], data['value'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/settings/api/delete', methods=['POST'])
@requires_tier('admin')
def settings_delete():
    db.delete_setting(GUILD_ID, request.json['key'])
    return jsonify({'ok': True})


# ── Section: Ticket Ephemerals ────────────────────────────────────────────────

@app.route('/friez/dashboard/ticket-ephemerals/')
@requires_tier('admin')
def section_ticket_ephemerals():
    types     = db.get_ticket_types(GUILD_ID)
    questions = {}
    for t in types:
        questions[t['name']] = db.get_ticket_questions(GUILD_ID, t['name'])
    return render_template('sections/ticket_ephemerals.html', user=g.user, tier=g.tier,
                           types=types, questions=questions)

@app.route('/friez/dashboard/ticket-ephemerals/api/add', methods=['POST'])
@requires_tier('admin')
def ticket_ephemerals_add():
    data = request.json
    db.add_ticket_question(GUILD_ID, data['type_name'], data['question'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/ticket-ephemerals/api/remove', methods=['POST'])
@requires_tier('admin')
def ticket_ephemerals_remove():
    db.remove_ticket_question(request.json['id'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/ticket-ephemerals/api/clear', methods=['POST'])
@requires_tier('admin')
def ticket_ephemerals_clear():
    db.clear_ticket_questions(GUILD_ID, request.json['type_name'])
    return jsonify({'ok': True})


# ── Section: Appeal Ephemeral ─────────────────────────────────────────────────

@app.route('/friez/dashboard/appeal-ephemeral/')
@requires_tier('admin')
def section_appeal_ephemeral():
    questions = db.get_appeal_questions()
    return render_template('sections/appeal_ephemeral.html', user=g.user, tier=g.tier,
                           questions=questions)

@app.route('/friez/dashboard/appeal-ephemeral/api/add', methods=['POST'])
@requires_tier('admin')
def appeal_ephemeral_add():
    db.add_appeal_question(request.json['question'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/appeal-ephemeral/api/remove', methods=['POST'])
@requires_tier('admin')
def appeal_ephemeral_remove():
    db.remove_appeal_question(request.json['id'])
    return jsonify({'ok': True})

@app.route('/friez/dashboard/appeal-ephemeral/api/clear', methods=['POST'])
@requires_tier('admin')
def appeal_ephemeral_clear():
    db.clear_appeal_questions()
    return jsonify({'ok': True})


# ── Section: Tickets ─────────────────────────────────────────────────────────

@app.route('/friez/dashboard/tickets/')
@requires_tier('mod')
def section_tickets():
    count = db.ticket_get_count(GUILD_ID)
    return render_template('sections/tickets.html', user=g.user, tier=g.tier, ticket_count=count)

@app.route('/friez/dashboard/tickets/api/log')
@requires_tier('mod')
def tickets_get_log():
    ticket_id = request.args.get('ticket_id', '').strip()
    if not ticket_id:
        ticket_id = db.ticket_latest_id(GUILD_ID)
    if not ticket_id:
        return jsonify({'error': 'No logs found'}), 404
    log = db.ticket_get_log(GUILD_ID, ticket_id)
    if not log:
        return jsonify({'error': 'Log not found'}), 404
    return jsonify({'ticket_id': ticket_id, 'transcript': log})


# ── Devtools ──────────────────────────────────────────────────────────────────
# Tier system:
#   public    — anyone, logged out fine
#   member    — must be logged in (any tier)
#   sensitive — dev OR granted access (else 418)

def _devtools_check_sensitive():
    """Abort 418 unless dev or explicitly granted devtools access."""
    uinfo, tier = _get_session_user()
    if tier == 'dev':
        return uinfo, tier
    uid = int(uinfo.get('id', 0)) if uinfo else 0
    if uid and db.has_devtools_access(uid):
        return uinfo, tier
    from werkzeug.exceptions import abort
    abort(418)

def _devtools_check_member():
    """Abort 418 unless logged in."""
    uinfo, tier = _get_session_user()
    if not uinfo:
        from werkzeug.exceptions import abort
        abort(418)
    return uinfo, tier

@app.route('/friez/devtools/')
def devtools_index():
    uinfo, tier = _get_session_user()
    granted = db.has_devtools_access(int(uinfo.get('id', 0))) if uinfo else False
    is_sensitive = (tier == 'dev' or granted)
    return render_template('devtools_index.html', user=uinfo, tier=tier,
                           is_sensitive=is_sensitive)

@app.route('/friez/devtools/errors')
def devtools_errors():
    uinfo, tier = _get_session_user()
    granted = db.has_devtools_access(int(uinfo.get('id', 0))) if uinfo else False
    is_sensitive = (tier == 'dev' or granted)
    return render_template('devtools_errors.html', user=uinfo, tier=tier,
                           is_sensitive=is_sensitive)

@app.route('/friez/devtools/trigger/<int:code>')
def devtools_trigger(code):
    _devtools_check_sensitive()
    from werkzeug.exceptions import abort
    abort(code)

@app.route('/friez/devtools/botstatus')
def devtools_botstatus():
    uinfo, tier = _get_session_user()
    return render_template('devtools_botstatus.html', user=uinfo, tier=tier)

@app.route('/friez/devtools/leaderboard')
def devtools_leaderboard():
    _devtools_check_member()
    uinfo, tier = _get_session_user()
    top = db.get_fc_leaderboard(20)
    return render_template('devtools_leaderboard.html', user=uinfo, tier=tier, top=top)

# ── Dashboard: devtools access management (dev only) ─────────────────────────

@app.route('/friez/devtools/api/ping')
def devtools_api_ping():
    try:
        db.get_all_devtools_access()
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({'db': db_ok})


@app.route('/friez/dashboard/devtools/')
@requires_tier('dev')
def section_devtools():
    users = db.get_all_devtools_access()
    return render_template('sections/devtools.html', user=g.user, tier=g.tier, users=users)

@app.route('/friez/dashboard/devtools/api/grant', methods=['POST'])
@requires_tier('dev')
def devtools_grant():
    uid = request.json.get('user_id')
    db.grant_devtools_access(uid)
    return jsonify({'ok': True})

@app.route('/friez/dashboard/devtools/api/revoke', methods=['POST'])
@requires_tier('dev')
def devtools_revoke():
    uid = request.json.get('user_id')
    db.revoke_devtools_access(uid)
    return jsonify({'ok': True})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dur(raw: str) -> float:
    import re
    m = re.fullmatch(r'(\d+)(s|m|h|d|mon)', raw.strip().lower())
    if not m: return 0
    val, unit = int(m.group(1)), m.group(2)
    return val * {'s':1,'m':60,'h':3600,'d':86400,'mon':2592000}[unit]


if __name__ == '__main__':
    db.init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
