from datetime import datetime, timedelta
import motor.motor_asyncio
from aiohttp import web
import aiohttp_jinja2
import mcstatus
import aiohttp
import asyncio
import jinja2
import json
import time
import os
import re

ip = os.getenv('ip')

if not ip:
	import dotenv
	dotenv.load_dotenv()
	ip = os.getenv('ip')

if not ip:
	raise ValueError('IP not found in .env!')

twitch_client_id = os.getenv('twitch_client_id')
twitch_token = os.getenv('twitch_token')

routes = web.RouteTableDef()
online_players = []

server = mcstatus.MinecraftServer.lookup(ip)
client = motor.motor_asyncio.AsyncIOMotorClient(os.getenv('dburi'))

online_coll = client.dreamsmp.online
players_coll = client.dreamsmp.players

player_list = None
uuids_to_minutes_played = {}

s = aiohttp.ClientSession()

async def check_online():
	status = await server.async_status()
	players = []
	for player in status.players.sample:
		uuid = player.id.replace('-', '')
		await add_new_player_if_unknown(player.name, uuid)
		live = await check_streaming_from_uuid(uuid)
		players.append({
			'name': player.name,
			'uuid': uuid,
			'live': live
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
		with open('html.html', 'w') as f:
			f.write(html)
		data = json.loads(re.findall(r'window\["ytInitialData"\] = (.+?);\n', html)[0])
		featured = data['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer']['content']['sectionListRenderer']['contents'][0]['itemSectionRenderer']['contents'][0]
		if 'channelFeaturedContentRenderer' not in featured:
			return False
		featured_video = featured['channelFeaturedContentRenderer']['items'][0]['videoRenderer']
		viewer_text = featured_video['viewCountText']['runs']
		live = viewer_text[1]['text'] == ' watching'
		viewers = int(viewer_text[0]['text'].replace(',', ''))
		title = featured_video['title']['runs'][0]['text']
		print(title)
		return live


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
	return len(data) > 0


def uuid_to_twitch_id(uuid):
	for player in player_list:
		if player['uuid'] == uuid:
			return player.get('twitch')

def uuid_to_youtube_id(uuid):
	for player in player_list:
		if player['uuid'] == uuid:
			return player.get('youtube')

async def check_streaming_from_uuid(uuid):
	uuid = uuid.replace('-', '')
	twitch_id = uuid_to_twitch_id(uuid)
	youtube_id = uuid_to_youtube_id(uuid)
	if twitch_id:
		if await check_streaming_twitch(twitch_id):
			return True
	if youtube_id:
		if await check_streaming_youtube(youtube_id):
			return True
	return False

async def add_online(uuids, live_uuids):
	await online_coll.insert_one({
		'time': datetime.now(),
		'players': uuids,
		'live': live_uuids
	})
	for uuid in uuids:
		if uuid not in uuids_to_minutes_played:
			uuids_to_minutes_played[uuid] = 0
		uuids_to_minutes_played[uuid] += 1

async def fetch_players():
	global player_list
	players = []
	async for player in players_coll.find({}):
		players.append({
			'username': player['username'],
			'uuid': player['uuid'],
			'twitch': player.get('twitch'),
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
	print(online_players)
	while True:
		await asyncio.sleep(60 - (time.time() % 60))
		try:
			players = await check_online()
			online_players = players
			print(online_players)
			uuids = []
			live_uuids = []
			for player in players:
				uuids.append(player['uuid'].replace('-', ''))
				if player['live']:
					live_uuids.append(player['uuid'].replace('-', ''))
			await add_online(uuids, live_uuids)
		except Exception as e:
			print(type(e), e)

async def get_history():
	history = []
	async for state in (
		online_coll
		.find({'time': {'$gt': datetime.now() - timedelta(hours=24)}})
		.sort('time', -1)
	):
		state['players'] = sorted(state['players'])
		history.append(
			state
		)
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
	print(uuids_to_minutes_played)

@routes.get('/')
@aiohttp_jinja2.template('index.html')
async def index(request):
	global online_players
	global player_list
	history = await get_history()
	online_players_set = set(player['uuid'].replace('-', '') for player in online_players)
	print(online_players_set)
	offline_players = []
	for player in player_list:
		if player['uuid'].replace('-', '') not in online_players_set:
			offline_players.append(player)
	return {
		'online': online_players,
		'offline': offline_players,
		'history': history,
		'players_list': player_list,
		'playtimes': uuids_to_minutes_played
	}


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

jinja_env.filters['minutes'] = minutes_to_string
jinja_env.filters['playtimesort'] = playtime_sort
web.run_app(app)