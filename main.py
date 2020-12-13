from datetime import datetime, timedelta
import motor.motor_asyncio
from aiohttp import web
import aiohttp_jinja2
import traceback
import mcstatus
import aiohttp
import asyncio
import jinja2
import json
import time
import os
import re

if not os.getenv('token'):
	from dotenv import load_dotenv
	load_dotenv()

# ip = os.getenv('ip')

# if not ip:
# 	import dotenv
# 	dotenv.load_dotenv()
# 	ip = os.getenv('ip')

# if not ip:
# 	raise ValueError('IP not found in .env!')

ip = None

twitch_client_id = os.getenv('twitch_client_id')
twitch_token = os.getenv('twitch_token')

routes = web.RouteTableDef()
online_players = []

if ip:
	server = mcstatus.MinecraftServer.lookup(ip)
else:
	server = None
client = motor.motor_asyncio.AsyncIOMotorClient(os.getenv('dburi'))

online_coll = client.dreamsmp.online
players_coll = client.dreamsmp.players

player_list = None
server_latency = None
uuids_to_minutes_played = {}

history = []

playercount = 0
maxplayers = 120

s = aiohttp.ClientSession()

async def check_online():
	global server_latency
	global playercount
	global maxplayers
	if ip:
		status = await server.async_status()
		players = []
		server_latency = status.latency
		playercount = status.players.online
		maxplayers = status.players.max
	else:
		status = None
		players = []
		server_latency = None
		playercount = None
		maxplayers = None

	using_sample = False
	
	if ip:
		sample = status.players.sample or []
		using_sample = True
	else:
		sample = player_list

	for player in sample:
		uuid = player.id if using_sample else player['uuid']
		uuid = uuid.replace('-', '')
		if ip:
			await add_new_player_if_unknown(player.name, uuid)
		live = await check_streaming_from_uuid(uuid)
		player_name = player.name if using_sample else player['username']
		if not ip and not live['live']: continue

		likely_dream_smp = False
		if ip:
			likely_dream_smp = True
		elif 'title' in live:
			if 'dreamsmp' in live['title'].lower().replace(' ', ''):
				likely_dream_smp = True
		players.append({
			'name': player_name,
			'uuid': uuid,
			'live': live['live'],
			'live_url': live.get('url'),
			'live_title': live.get('title'),
			'likely_on_server': likely_dream_smp
		})
	return players


async def check_streaming_youtube(youtube_id):
	url = f'https://www.youtube.com/channel/{youtube_id}' if youtube_id.startswith('UC') else f'https://www.youtube.com/c/{youtube_id}'
	async with s.get(
		url,
		headers={
			'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0'
		}
	) as r:
		html = await r.text()
		data = json.loads(re.findall(r'(?:window\["ytInitialData"\]|var ytInitialData) = (.+?);', html)[0])
		featured = data['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer']['content']['sectionListRenderer']['contents'][0]['itemSectionRenderer']['contents'][0]
		if 'channelFeaturedContentRenderer' not in featured:
			return {
				'live': False
			}
		featured_video = featured['channelFeaturedContentRenderer']['items'][0]['videoRenderer']
		viewer_text = featured_video['viewCountText']['runs']
		live = viewer_text[1]['text'] == ' watching'
		if not live:
			return {
				'live': False
			}
		viewers = int(viewer_text[0]['text'].replace(',', ''))
		title = featured_video['title']['runs'][0]['text']
		return {
			'live': True,
			'title': title,
			'viewers': viewers
		}


async def check_streaming_twitch(channel_id):
	async with s.get(
		f'https://api.twitch.tv/helix/streams?user_id={channel_id}',
		headers={
			'Client-Id': twitch_client_id,
			'Authorization': 'Bearer ' + twitch_token
		}
	) as r:
		data = await r.json()
	data = data['data']
	if len(data) > 0:
		stream = data[0]
		title = stream['title']
		viewers = stream['viewer_count']
		return {
			'live': True,
			'title': title,
			'viewers': viewers
		}
	else:
		return {
			'live': False
		}


def uuid_to_twitch_id(uuid):
	for player in player_list:
		if player['uuid'] == uuid:
			return player.get('twitch')

def uuid_to_twitch_name(uuid):
	for player in player_list:
		if player['uuid'] == uuid:
			return player.get('twitch_name')

def uuid_to_youtube_id(uuid):
	for player in player_list:
		if player['uuid'] == uuid:
			return player.get('youtube')

async def check_streaming_from_uuid(uuid):
	uuid = uuid.replace('-', '')
	twitch_id = uuid_to_twitch_id(uuid)
	youtube_id = uuid_to_youtube_id(uuid)
	if twitch_id:
		twitch_stream_data = await check_streaming_twitch(twitch_id)
		if twitch_stream_data['live']:
			twitch_name = uuid_to_twitch_name(uuid)
			if twitch_name:
				url = 'https://www.twitch.tv/' + twitch_name
			else:
				url = None
			return {
				'live': True,
				'url': url,
				'title': twitch_stream_data['title'],
				'viewers': twitch_stream_data['viewers'],
			}
	if youtube_id:
		youtube_stream_data = await check_streaming_youtube(youtube_id)
		if youtube_stream_data['live']:
			return {
				'live': True,
				'title': youtube_stream_data['title'],
				'viewers': youtube_stream_data['viewers']
			}
	return {
		'live': False
	}

async def add_online(uuids, live_uuids, live_titles):
	new_doc = {
		'time': datetime.now(),
		'players': uuids,
		'live': live_uuids,
		'titles': live_titles
	}
	await online_coll.insert_one(new_doc)
	for uuid in uuids:
		if uuid not in uuids_to_minutes_played:
			uuids_to_minutes_played[uuid] = 0
		uuids_to_minutes_played[uuid] += 1
	if history:
		history.insert(0, new_doc)

async def fetch_players():
	global player_list
	players = []
	async for player in players_coll.find({}):
		players.append({
			'username': player['username'],
			'uuid': player['uuid'],
			'twitch': player.get('twitch'),
			'twitch_name': player.get('twitch_name'),
			'youtube': player.get('youtube'),
		})
	player_list = players
	return players

async def add_new_player_if_unknown(username, uuid):
	global player_list
	uuid = uuid.replace('-', '')
	if player_list is None:
		print('No player list yet, probably still fetching')
		return
	for player in player_list:
		if player['uuid'] == uuid and player['username'] == username:
			return
	print('Added new player', uuid)
	await players_coll.update_one(
		{
			'uuid': uuid
		},
		{
			'$set': {'username': username},
		},
		upsert=True
	)
	player_list.append({
		'uuid': uuid,
		'username': username
	})

async def check_server_task():
	global online_players
	await fetch_players()
	await cache_members_playtime()
	players = await check_online()
	online_players = players
	while True:
		await asyncio.sleep(60 - (time.time() % 60))
		try:
			players = await check_online()
			online_players = players
			uuids = []
			live_uuids = []
			live_titles = {}
			for player in players:
				uuid = player['uuid'].replace('-', '')
				uuids.append(uuid)
				if player['live']:
					live_uuids.append(uuid)
					live_titles[uuid] = player['live_title']
			await add_online(uuids, live_uuids, live_titles)
		except Exception as e:
			print(type(e), e)
			traceback.print_tb()

def combine_history_states(states):
	combined_state = {
		'players': set(),
		'live': set(),
		'titles': {},
		'time': datetime.now(),
	}
	for state in states:
		combined_state['players'].update(set(state['players']))
		combined_state['live'].update(set(state['live']))
		combined_state['titles'].update(state['titles'])
		if state['time'] < combined_state['time']:
			combined_state['time'] = state['time']
	return combined_state

async def get_history():
	global history
	if history:
		return history
	new_history = []
	prev_state = {}
	actual_prev_state = {}
	removing_ids = []
	temp_states = []
	history = [None]

	# the max amount of days ago it should get data from
	get_before = datetime.now() - timedelta(days=30)
	# when it should stop only getting the peaks
	simplify_before = datetime.now() - timedelta(days=1)

	last_simplified = get_before
	async for state in (
		online_coll
		# .find({}, batch_size=1000)
		.find({'time': {'$gt': get_before}}, batch_size=1000)
		.sort('time', 1)
	):
		state['players'] = sorted(state['players'])
		state['live'] = sorted(state.get('live', []))
		state['titles'] = state.get('titles') or {}

		if state['time'] < simplify_before:
			temp_states.append(state)
			last_simplified_ago = state['time'] - last_simplified
			if last_simplified_ago > timedelta(hours=1):
				last_simplified = state['time']
				state = combine_history_states(temp_states)
				temp_states = []
			else:
				continue


		state_without_time = {
			'players': state['players'],
			'live': state['live'],
			'titles': state['titles'],
		}

		if state_without_time == prev_state:
			actual_prev_state = state
			continue

		if actual_prev_state:
			new_history.append(
				actual_prev_state
			)
		new_history.append(
			state
		)
		prev_state = state_without_time
		actual_prev_state = None

	print('deleting', len(removing_ids))
	# await online_coll.delete_many({'_id': {'$in': removing_ids}}) # delete duplicates

	history = list(reversed(new_history))
	print('gotten history of', len(history))
	return history

async def get_member_playtime(uuid):
	global uuids_to_minutes_played
	uuid = uuid.replace('-', '')
	total_minutes = await online_coll.count_documents({'players': { '$in': [uuid] } })
	uuids_to_minutes_played[uuid] = total_minutes
	return total_minutes

async def cache_members_playtime():
	for member in player_list:
		await get_member_playtime(member['uuid'])

@routes.get('/')
@aiohttp_jinja2.template('index.html')
async def index(request):
	global online_players
	global player_list
	print('getting history')
	player_history = await get_history()
	if player_history == [None]:
		return
	online_players_set = set(player['uuid'].replace('-', '') for player in online_players)
	offline_players = []
	for player in player_list:
		if player['uuid'].replace('-', '') not in online_players_set:
			offline_players.append(player)
	print('ok')
	return {
		'online': online_players,
		'offline': offline_players,
		'history': player_history,
		'players_list': player_list,
		'playtimes': uuids_to_minutes_played,
		'latency': server_latency,
		'playercount': playercount,
		'maxplayers': maxplayers
	}

sitemap_dict = {
	'/': {
		'priority': 1.0,
		'lastmod': datetime.strftime(datetime.now(), '%Y-%m-%dT%H:%I:%SZ')
	}
}

@routes.get('/sitemap.xml')
async def sitemap(request):
	context = {
		'sitemap': sitemap_dict
	}
	response = aiohttp_jinja2.render_template(
		'sitemap.xml',
		request,
		context
	)
	response.headers['content-type'] = 'application/xml'
	return response

if os.getenv('dev') == 'true':
	asyncio.ensure_future(check_server_task())

app = web.Application()

app.add_routes(routes)

jinja_env = aiohttp_jinja2.setup(
	app,
	loader=jinja2.FileSystemLoader('templates')
)

def minutes_to_string(minutes):
	if minutes < 60:
		if minutes == 1:
			return '1 minute'
		else:
			return f'{minutes} minutes'
	else:
		hours = minutes // 60
		if hours == 1:
			return '1 hour'
		else:
			return f'{hours} hours'

def playtime_sort(items):
	return sorted(
		items,
		key=lambda p: uuids_to_minutes_played.get(p['uuid'], 0),
		reverse=True
	)

def uuid_to_playtime(uuid):
	if uuid not in uuids_to_minutes_played:
		return '0 minutes'
	else:
		return minutes_to_string(uuids_to_minutes_played[uuid])

jinja_env.filters['minutes'] = minutes_to_string
jinja_env.filters['playtimesort'] = playtime_sort
jinja_env.globals['playtime'] = uuid_to_playtime
jinja_env.globals['streamingsvg'] = '''<span class="liveicon"><svg width="1em" height="1em"><circle stroke="black" stroke-width="3" fill="red" r=".5em" cx=".5em" cy=".5em"></circle></svg></span>'''
jinja_env.globals['orangecirclesvg'] = '''<span class="liveicon"><svg width="1em" height="1em"><circle stroke="black" stroke-width="3" fill="orange" r=".5em" cx=".5em" cy=".5em"></circle></svg></span>'''
web.run_app(app, host=os.getenv('host'))

