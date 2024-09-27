from typing import Any

import aiohttp
import asyncio
import fasteners
import os
import random
from urllib.parse import unquote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from datetime import datetime, timedelta
from time import time

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputUser, InputBotAppShortName, InputPeerUser, InputNotifyPeer, InputPeerNotifySettings
from telethon.functions import messages, contacts, channels, account

from .agents import generate_random_user_agent
from .headers import *
from bot.config import settings
from bot.utils import logger, log_error, proxy_utils, config_utils, CONFIG_PATH
from bot.utils.transaction import TRANSACTION_METHODS
from bot.exceptions import InvalidSession


class Tapper:
    def __init__(self, tg_client: TelegramClient):
        self.session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        self.tg_client = tg_client
        self.auth_data = ''
        self.hash = ''
        self.tg_id = ''
        self.config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        self.proxy = self.config.get('proxy', None)
        self.lock = fasteners.InterProcessLock(os.path.join(os.path.dirname(CONFIG_PATH), 'lock_files',  f"{self.session_name}.lock"))
        self.headers = headers
        self.headers['User-Agent'] = self.check_user_agent()
        self.headers.update(**get_sec_ch_ua(self.headers.get('User-Agent', '')))

        self._webview_data = None

    def log_message(self, message) -> str:
        return f"<light-yellow>{self.session_name}</light-yellow> | {message}"

    def check_user_agent(self):
        user_agent = self.config.get('user_agent')
        if not user_agent:
            user_agent = generate_random_user_agent()
            self.config['user_agent'] = user_agent
            config_utils.update_session_config_in_file(self.session_name, self.config, CONFIG_PATH)

        return user_agent

    async def get_tg_web_data(self) -> [str | None]:
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
        else:
            proxy_dict = None
        self.tg_client.set_proxy(proxy_dict)

        tg_web_data = None
        with self.lock:
            async with self.tg_client as client:
                if not self._webview_data:
                    while True:
                        try:
                            peer = await client.get_input_entity('boolfamily_Bot')
                            input_bot_app = InputBotAppShortName(bot_id=peer, short_name="join")
                            self._webview_data = {'peer': peer, 'app': input_bot_app}
                            break
                        except FloodWaitError as fl:
                            fls = fl.seconds

                            logger.warning(self.log_message(f"FloodWait {fl}"))
                            logger.info(self.log_message(f"Sleep {fls}s"))
                            await asyncio.sleep(fls + 3)

                ref_id = settings.REF_ID if random.randint(0, 100) <= 85 else "8T1K2"

                web_view = await client(messages.RequestAppWebViewRequest(
                    **self._webview_data,
                    platform='android',
                    write_allowed=True,
                    start_param=ref_id
                ))

                auth_url = web_view.url
                tg_web_data = unquote(
                    string=unquote(string=auth_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]))

                user_data = re.findall(r'user=([^&]+)', tg_web_data)[0]
                chat_instance = re.findall(r'chat_instance=([^&]+)', tg_web_data)[0]
                chat_type = re.findall(r'chat_type=([^&]+)', tg_web_data)[0]
                start_param = '\nstart_param=' + re.findall(r'start_param=([^&]+)', tg_web_data)[0]
                auth_date = re.findall(r'auth_date=([^&]+)', tg_web_data)[0]
                hash_value = re.findall(r'hash=([^&]+)', tg_web_data)[0]

                user = user_data.replace('"', '\"')
                self.auth_data = f"auth_date={auth_date}\nchat_instance={chat_instance}\nchat_type={chat_type}{start_param}\nuser={user}"
                self.hash = hash_value
                self.tg_id = user.split('"id":')[1].split(',')[0]

        return tg_web_data

    async def get_strict_data(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post('https://miniapp.bool.network/backend/bool-tg-interface/user/user/strict',
                                              json=json_data)
            response.raise_for_status()

            response_json = await response.json()
            await http_client.get(
                f'https://miniapp.bool.network/backend/bool-tg-interface/user/check?tgId={self.tg_id}')
            return response_json['data']

        except Exception as error:
            log_error(self.log_message(f"Unknown error when getting user strict data: {error}"))
            await asyncio.sleep(delay=random.randint(3, 7))

    async def register_user(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post('https://bot-api.bool.network/bool-tg-interface/user/register',
                                              json=json_data)
            response.raise_for_status()

            response_json = await response.json()
            if response_json['message'] == "success":
                logger.success(self.log_message(
                    f"User successfully registered | User Id: <y>{response_json['data']}</y>"))

            return response_json['data']

        except Exception as error:
            log_error(self.log_message(f"Unknown error during registration: {error}"))
            await asyncio.sleep(delay=random.randint(3, 7))

    async def check_proxy(self, http_client: aiohttp.ClientSession) -> bool:
        proxy_conn = http_client._connector
        try:
            response = await http_client.get(url='https://ifconfig.me/ip', timeout=aiohttp.ClientTimeout(15))
            logger.info(self.log_message(f"Proxy IP: {await response.text()}"))
            return True
        except Exception as error:
            proxy_url = f"{proxy_conn._proxy_type}://{proxy_conn._proxy_host}:{proxy_conn._proxy_port}"
            log_error(self.log_message(f"Proxy: {proxy_url} | Error: {type(error).__name__}"))
            return False

    async def do_task(self, http_client: aiohttp.ClientSession, task_name: str, task_id: int):
        try:
            logger.info(self.log_message(f"Performing task <lc>{task_name}</lc>..."))

            json_data = {
                "assignmentId": task_id,
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post(f'https://bot-api.bool.network/bool-tg-interface/assignment/do',
                                              json=json_data)
            response.raise_for_status()
            response_json = await response.json()

            return response_json['data']

        except Exception as error:
            log_error(self.log_message(f"Unknown error when processing task: {error}"))
            await asyncio.sleep(delay=3)

    async def get_user_staking(self, http_client: aiohttp.ClientSession, wallet_address: str) -> dict:
        try:
            payload = {
                'ownerAddress': wallet_address,
                'pageNo': 1,
                'pageSize': 200,
                'yield': 1
            }
            response = await http_client.get(f'https://beta-api.boolscan.com/bool-network-beta/blockchain/devices-vote',
                                             params=payload)
            response.raise_for_status()
            response_json = await response.json()

            return response_json['data']['items']

        except Exception as error:
            log_error(self.log_message(f"Unknown error when getting user staking: {error}"))
            await asyncio.sleep(delay=3)

    async def get_staking_record(self, http_client: aiohttp.ClientSession, page: int) -> dict | None:
        try:
            response = await http_client.get(
                f'https://miniapp.bool.network/backend/bool-tg-interface/user/vote:devices?'
                f'pageNo={page}&pageSize=20&yield=1')
            response.raise_for_status()
            response_json = await response.json()

            records = response_json['data']['records']
            for record in records:
                if record['voterCount'] < 500 and record['deviceState'] == 'SERVING':
                    return record

            if page < int(response_json['data']['pages']):
                return await self.get_staking_record(http_client, page + 1)
            else:
                logger.warning(self.log_message(f"Failed getting staking record"))
                return None

        except Exception as error:
            log_error(self.log_message(f"Unknown error when getting records: {error}"))
            await asyncio.sleep(delay=3)

    async def verify_account(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }

            response = await http_client.post('https://miniapp.bool.network/backend/bool-tg-interface/user/verify',
                                              json=json_data)
            response.raise_for_status()
            response_json = await response.json()
            if response_json['data']:
                logger.success(self.log_message(f"Account Verified! Available for tBOL Airdrop"))
            else:
                logger.warning(self.log_message(f"Failed verified account | Try next time"))

        except Exception as error:
            log_error(self.log_message(f"Unknown error when verifying account: {error}"))
            await asyncio.sleep(delay=3)

    async def make_staking(self, http_client: aiohttp.ClientSession, device_id: str, amount: int):
        try:
            json_data = {
                "deviceId": [device_id],
                "amount": [amount],
                "data": self.auth_data,
                "hash": self.hash
            }

            response = await http_client.post('https://bot-api.bool.network/bool-tg-interface/stake/do',
                                              json=json_data)
            response.raise_for_status()
            response_json = await response.json()
            if response_json['message'] == "success":
                return response_json['data']
            else:
                logger.warning(self.log_message(f"Failed making stake <r>{response_json['message']}</r>"))

        except Exception as error:
            log_error(self.log_message(f"Unknown error when staking: {error}"))
            await asyncio.sleep(delay=3)

    async def get_staking_balance(self, http_client: aiohttp.ClientSession, wallet_address: str):
        try:

            chain_id = TRANSACTION_METHODS['eth_chainId']
            chain_id['id'] = 1
            get_balance = TRANSACTION_METHODS['eth_getBalance']
            get_balance['id'] = 2
            get_balance['params'][0] = wallet_address
            json_data = [chain_id, get_balance]
            response = await http_client.post('https://betatest-rpc-node-http.bool.network/',
                                              json=json_data)
            response.raise_for_status()
            transaction_data = await response.json()
            hex_balance = transaction_data[1].get('result')
            dec_balance = int(hex_balance, 16)
            return int(dec_balance / 1e18)

        except Exception as error:
            log_error(self.log_message(f"Unknown error when getting staking balance: {error}"))
            await asyncio.sleep(delay=3)

    async def send_transaction_data(self, http_client: aiohttp.ClientSession, json_data: Any):
        try:
            response = await http_client.post('https://betatest-rpc-node-http.bool.network/',
                                              json=json_data)
            response.raise_for_status()
            response_json = await response.json()
            return response_json
        except Exception as error:
            log_error(self.log_message(f"Unknown error when sending transaction data: {error}"))
            await asyncio.sleep(delay=3)

    async def performing_transaction(self, http_client: aiohttp.ClientSession, data: str, wallet_address: str):
        try:
            chain_id = TRANSACTION_METHODS['eth_chainId']
            get_block_number = TRANSACTION_METHODS['eth_blockNumber']
            send_transaction = TRANSACTION_METHODS['eth_sendRawTransaction']
            get_receipt = TRANSACTION_METHODS['eth_getTransactionReceipt']
            transaction_count = TRANSACTION_METHODS['eth_getTransactionCount']
            get_by_hash = TRANSACTION_METHODS['eth_getTransactionByHash']
            get_block = TRANSACTION_METHODS['eth_getBlockByNumber']

            chain_id['id'] = 1
            get_block_number['id'] = 2
            send_transaction['id'] = 3
            send_transaction['params'] = [data]
            json_data = [chain_id, get_block_number, send_transaction]
            transaction_data = await self.send_transaction_data(http_client=http_client, json_data=json_data)
            transaction = transaction_data[2]['result']

            if transaction:
                chain_id['id'] = 4
                get_receipt['id'] = 5
                get_receipt['params'] = [transaction]
                json_data = [chain_id, get_receipt]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                get_block_number['id'] = 6
                transaction_count['id'] = 7
                chain_id['id'] = 8
                transaction_count['params'][0] = wallet_address
                json_data = [get_block_number, transaction_count, chain_id]
                transaction_data = await self.send_transaction_data(http_client=http_client, json_data=json_data)
                block_number = transaction_data[0]['result']

                chain_id['id'] = 9
                get_by_hash['id'] = 10
                get_by_hash['params'] = [transaction]
                json_data = [chain_id, get_by_hash]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                chain_id['id'] = 11
                get_block['id'] = 12
                get_block['params'][0] = block_number
                json_data = [chain_id, get_block]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                chain_id['id'] = 13
                get_receipt['id'] = 14
                get_block_number['id'] = 15
                json_data = [chain_id, get_receipt, get_block_number]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                get_block_number['id'] = 16
                json_data = [get_block_number]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                get_block_number['id'] = 17
                transaction_count['id'] = 18
                chain_id['id'] = 19
                get_receipt['id'] = 20
                json_data = [get_block_number, transaction_count, chain_id, get_receipt]
                await self.send_transaction_data(http_client=http_client, json_data=json_data)

                chain_id['id'] = 21
                get_by_hash['id'] = 22
                json_data = [chain_id, get_by_hash]
                result_json = await self.send_transaction_data(http_client=http_client, json_data=json_data)
                return result_json[1]['result']

        except Exception as error:
            log_error(self.log_message(f"Unknown error when performing transaction: {error}"))
            await asyncio.sleep(delay=3)

    async def join_and_mute_tg_channel(self, link: str):
        path = link.replace("https://t.me/", "")
        if path == 'money':
            return

        with self.lock:
            async with self.tg_client as client:
                try:
                    if path.startswith('+'):
                        invite_hash = path[1:]
                        result = await client(messages.ImportChatInviteRequest(hash=invite_hash))
                        channel_title = result.chats[0].title
                        entity = result.chats[0]
                    else:
                        entity = await client.get_entity(f'@{path}')
                        await client(channels.JoinChannelRequest(channel=entity))
                        channel_title = entity.title

                    await asyncio.sleep(1)

                    await client(account.UpdateNotifySettingsRequest(
                        peer=InputNotifyPeer(entity),
                        settings=InputPeerNotifySettings(
                            show_previews=False,
                            silent=True,
                            mute_until=datetime.today() + timedelta(days=365)
                        )
                    ))

                    logger.info(self.log_message(f"Subscribe to channel: <y>{channel_title}</y>"))
                except Exception as e:
                    log_error(self.log_message(f"(Task) Error while subscribing to tg channel: {e}"))

    async def check_daily_reward(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post(
                f'https://miniapp.bool.network/backend/bool-tg-interface/assignment/daily/list',
                json=json_data)
            response.raise_for_status()
            response_json = await response.json()

            if len(response_json['data']) > 0:
                daily_task = response_json['data'][0]
                if not daily_task.get('done'):
                    # await self.join_tg_channel(daily_task['url'])
                    json_data['assignmentId'] = daily_task['assignmentId']
                    response = await http_client.post(
                        f'https://miniapp.bool.network/backend/bool-tg-interface/assignment/daily/do',
                        json=json_data)
                    response.raise_for_status()
                    logger.success(self.log_message(f"Daily claimed | Reward: <e>{daily_task['reward']}</e> tBOL | "
                                                    f"Day count: <e>{daily_task['signDay'] + 1}</e>"))

        except Exception as error:
            log_error(self.log_message(f"Unknown error when processing daily reward: {error}"))
            await asyncio.sleep(delay=3)

    async def processing_tasks(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post('https://miniapp.bool.network/backend/bool-tg-interface/assignment/list',
                                              json=json_data)
            response.raise_for_status()
            response_json = await response.json()

            tasks = response_json['data']
            for task in tasks:
                if not task['done'] and task['assignmentId'] != 48 and task['project'] != 'daily':
                    await asyncio.sleep(delay=random.randint(5, 15))
                    status = await self.do_task(http_client, task['title'], int(task['assignmentId']))
                    if status:
                        logger.success(self.log_message(f"Task <lc>{task['title']}</lc> - Completed | "
                                                        f"Reward: <e>{task['reward']}</e> tBOL"))
                    else:
                        logger.warning(self.log_message(f"Failed processing task <lc>{task['title']}</lc>"))

        except Exception as error:
            log_error(self.log_message(f"Unknown error when processing tasks: {error}"))
            await asyncio.sleep(delay=3)

    async def check_user_subscription(self, http_client: aiohttp.ClientSession):
        try:
            json_data = {
                "data": self.auth_data,
                "hash": self.hash
            }
            response = await http_client.post(
                'https://miniapp.bool.network/backend/bool-tg-interface/user/channel/joined',
                json=json_data)
            response.raise_for_status()
            response_json = await response.json()

            return response_json['data']

        except Exception as error:
            log_error(self.log_message(f"Unknown error when checking tg subscription: {error}"))
            await asyncio.sleep(delay=3)

    async def run(self) -> None:
        random_delay = random.randint(1, settings.RANDOM_DELAY_IN_RUN)
        logger.info(self.log_message(f"Bot will start in <ly>{random_delay}s</ly>"))
        await asyncio.sleep(random_delay)

        access_token_created_time = 0
        tg_web_data = None

        token_live_time = random.randint(3500, 3600)

        proxy_conn = {'connector': ProxyConnector.from_url(self.proxy)} if self.proxy else {}
        async with CloudflareScraper(headers=self.headers, timeout=aiohttp.ClientTimeout(60), **proxy_conn) as http_client:
            while True:
                if not await self.check_proxy(http_client=http_client):
                    logger.warning(self.log_message('Failed to connect to proxy server. Sleep 5 minutes.'))
                    await asyncio.sleep(300)
                    continue
                try:
                    sleep_time = random.randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1])
                    if time() - access_token_created_time >= token_live_time:
                        tg_web_data = await self.get_tg_web_data()

                        if not tg_web_data:
                            raise InvalidSession('Failed to get webview URL')

                        access_token_created_time = time()
                        token_live_time = random.randint(3500, 3600)

                        strict_data = await self.get_strict_data(http_client=http_client)
                        if strict_data is None:
                            await asyncio.sleep(delay=random.randint(1, 3))
                            user_id = await self.register_user(http_client=http_client)
                            if user_id is not None:
                                strict_data = await self.get_strict_data(http_client=http_client)

                        balance = strict_data['rewardValue']
                        rank = strict_data['rank']
                        logger.info(self.log_message(f"Balance: <e>{balance}</e> tBOL | "
                                                     f"Rank: <fg #ffbcd9>{rank}</fg #ffbcd9>"))

                        await self.check_daily_reward(http_client=http_client)

                        if settings.AUTO_TASK:
                            await asyncio.sleep(delay=random.randint(3, 5))
                            await self.processing_tasks(http_client=http_client)

                        if settings.STAKING:
                            await asyncio.sleep(delay=random.randint(3, 5))
                            balance = await self.get_staking_balance(http_client=http_client,
                                                                     wallet_address=strict_data['evmAddress'])
                            user_staking = await self.get_user_staking(http_client=http_client,
                                                                       wallet_address=strict_data['evmAddress'])

                            if user_staking is None or len(user_staking) == 0 and not strict_data['isVerify']:
                                if balance > settings.MIN_STAKING_BALANCE:
                                    record = await self.get_staking_record(http_client=http_client, page=1)
                                    if record is not None:
                                        voters = record['voterCount']
                                        apy = round(record['yield'] * 100, 2)
                                        logger.info(self.log_message(
                                            f'Staking record found | APY: <lc>{apy}%</lc> | Voters: <y>{voters}</y>'))
                                        await asyncio.sleep(delay=random.randint(3, 5))
                                        data = await self.make_staking(http_client=http_client,
                                                                       device_id=record['deviceID'], amount=balance)
                                        if data is not None:
                                            await asyncio.sleep(delay=random.randint(3, 5))
                                            result = await self.performing_transaction(http_client=http_client,
                                                                                       data=data,
                                                                                       wallet_address=strict_data[
                                                                                           'evmAddress'])
                                            if result is not None:
                                                logger.success(self.log_message(
                                                    f"Successfully staked <e>{balance}</e> tBOL"))

                            elif not strict_data['isVerify']:
                                await asyncio.sleep(delay=random.randint(3, 10))
                                subscribed = await self.check_user_subscription(http_client=http_client)
                                if not subscribed:
                                    await self.join_and_mute_tg_channel('https://t.me/boolofficial')
                                else:
                                    await self.verify_account(http_client=http_client)

                except InvalidSession as error:
                    raise error

                except Exception as error:
                    log_error(self.log_message(f"Unknown error: {error}"))
                    await asyncio.sleep(delay=random.randint(60, 120))

                else:
                    logger.info(f"{self.session_name} | Sleep <y>{round(sleep_time / 60, 1)}</y> min")
                    await asyncio.sleep(delay=sleep_time)


async def run_tapper(tg_client: TelegramClient):
    runner = Tapper(tg_client=tg_client)
    try:
        await runner.run()
    except InvalidSession as e:
        log_error(runner.log_message(f"Invalid Session: {e}"))
