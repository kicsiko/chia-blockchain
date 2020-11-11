import asyncio
import json
import time
from typing import Dict, Optional, Tuple, List, AsyncGenerator, Callable
import concurrent
from pathlib import Path
import random
import socket
import logging
import traceback
from blspy import PrivateKey

from src.server.ws_connection import WSChiaConnection
from src.types.peer_info import PeerInfo
from src.util.byte_types import hexstr_to_bytes
from src.util.merkle_set import (
    confirm_included_already_hashed,
    confirm_not_included_already_hashed,
    MerkleSet,
)
from src.protocols import introducer_protocol, wallet_protocol, full_node_protocol
from src.consensus.constants import ConsensusConstants
from src.server.server import ChiaServer
from src.server.outbound_message import OutboundMessage, NodeType, Message, Delivery
from src.server.node_discovery import WalletPeers
from src.util.ints import uint32, uint64, uint128
from src.types.sized_bytes import bytes32
from src.util.api_decorators import api_request
from src.wallet.derivation_record import DerivationRecord
from src.wallet.settings.settings_objects import BackupInitialized
from src.wallet.transaction_record import TransactionRecord
from src.wallet.util.backup_utils import open_backup_file
from src.wallet.util.wallet_types import WalletType
from src.wallet.wallet_action import WalletAction
from src.wallet.wallet_state_manager import WalletStateManager
from src.wallet.block_record import BlockRecord
from src.types.header_block import HeaderBlock
from src.types.full_block import FullBlock
from src.types.coin import Coin, hash_coin_list
from src.full_node.blockchain import ReceiveBlockResult
from src.types.mempool_inclusion_status import MempoolInclusionStatus
from src.util.errors import Err
from src.util.path import path_from_root, mkdir
from src.util.keychain import Keychain

OutboundMessageGenerator = AsyncGenerator[OutboundMessage, None]


class WalletNode:
    key_config: Dict
    config: Dict
    constants: ConsensusConstants
    server: Optional[ChiaServer]
    log: logging.Logger
    wallet_peers: WalletPeers
    # Maintains the state of the wallet (blockchain and transactions), handles DB connections
    wallet_state_manager: Optional[WalletStateManager]

    # Maintains headers recently received. Once the desired removals and additions are downloaded,
    # the data is persisted in the WalletStateManager. These variables are also used to store
    # temporary sync data. The bytes is the transaction filter.
    cached_blocks: Dict[bytes32, Tuple[BlockRecord, HeaderBlock, bytes]]

    # Prev hash to curr hash
    future_block_hashes: Dict[bytes32, bytes32]

    # Hashes of the PoT and PoSpace for all blocks (including occasional difficulty adjustments)
    proof_hashes: List[Tuple[bytes32, Optional[uint64], Optional[uint64]]]

    # List of header hashes downloaded during sync
    header_hashes: List[bytes32]
    header_hashes_error: bool

    # Event to signal when a block is received (during sync)
    potential_blocks_received: Dict[uint32, asyncio.Event]
    potential_header_hashes: Dict[uint32, bytes32]

    # How far away from LCA we must be to perform a full sync. Before then, do a short sync,
    # which is consecutive requests for the previous block
    short_sync_threshold: int
    _shut_down: bool
    root_path: Path
    state_changed_callback: Optional[Callable]

    def __init__(
        self,
        config: Dict,
        keychain: Keychain,
        root_path: Path,
        consensus_constants: ConsensusConstants,
        name: str = None,
    ):
        self.config = config
        self.constants = consensus_constants
        self.root_path = root_path
        if name:
            self.log = logging.getLogger(name)
        else:
            self.log = logging.getLogger(__name__)

        # Normal operation data
        self.cached_blocks = {}
        self.future_block_hashes = {}
        self.keychain = keychain

        # Sync data
        self._shut_down = False
        self.proof_hashes = []
        self.header_hashes = []
        self.header_hashes_error = False
        self.short_sync_threshold = 15  # Change the test when changing this
        self.potential_blocks_received = {}
        self.potential_header_hashes = {}
        self.state_changed_callback = None
        self.wallet_state_manager = None
        self.backup_initialized = False  # Delay first launch sync after user imports backup info or decides to skip
        self.server = None
        self.wsm_close_task = None

    def get_key_for_fingerprint(self, fingerprint):
        private_keys = self.keychain.get_all_private_keys()
        if len(private_keys) == 0:
            self.log.warning(
                "No keys present. Create keys with the UI, or with the 'chia keys' program."
            )
            return None

        private_key: Optional[PrivateKey] = None
        if fingerprint is not None:
            for sk, _ in private_keys:
                if sk.get_g1().get_fingerprint() == fingerprint:
                    private_key = sk
                    break
        else:
            private_key = private_keys[0][0]
        return private_key

    async def _start(
        self,
        fingerprint: Optional[int] = None,
        new_wallet: bool = False,
        backup_file: Optional[Path] = None,
        skip_backup_import: bool = False,
    ) -> bool:
        private_key = self.get_key_for_fingerprint(fingerprint)
        if private_key is None:
            return False

        db_path_key_suffix = str(private_key.get_g1().get_fingerprint())
        path = path_from_root(
            self.root_path, f"{self.config['database_path']}-{db_path_key_suffix}"
        )
        mkdir(path.parent)

        self.wallet_state_manager = await WalletStateManager.create(
            private_key, self.config, path, self.constants
        )

        self.wsm_close_task = None
        assert self.wallet_state_manager is not None

        backup_settings: BackupInitialized = (
            self.wallet_state_manager.user_settings.get_backup_settings()
        )
        if backup_settings.user_initialized is False:
            if new_wallet is True:
                await self.wallet_state_manager.user_settings.user_created_new_wallet()
                self.wallet_state_manager.new_wallet = True
            elif skip_backup_import is True:
                await self.wallet_state_manager.user_settings.user_skipped_backup_import()
            elif backup_file is not None:
                await self.wallet_state_manager.import_backup_info(backup_file)
            else:
                self.backup_initialized = False
                await self.wallet_state_manager.close_all_stores()
                self.wallet_state_manager = None
                return False

        self.backup_initialized = True
        if backup_file is not None:
            json_dict = open_backup_file(
                backup_file, self.wallet_state_manager.private_key
            )
            if "start_height" in json_dict["data"]:
                start_height = json_dict["data"]["start_height"]
                self.config["starting_height"] = max(
                    0, start_height - self.config["start_height_buffer"]
                )
            else:
                self.config["starting_height"] = 0
        else:
            self.config["starting_height"] = 0

        if self.state_changed_callback is not None:
            self.wallet_state_manager.set_callback(self.state_changed_callback)

        self.wallet_state_manager.set_pending_callback(self._pending_tx_handler)
        self._shut_down = False

        asyncio.create_task(self._periodically_check_full_node())
        return True

    def _close(self):
        self._shut_down = True
        if self.wallet_state_manager is None:
            return
        self.wsm_close_task = asyncio.create_task(
            self.wallet_state_manager.close_all_stores()
        )
        self.wallet_peers_task = asyncio.create_task(
            self.wallet_peers.ensure_is_closed()
        )

    async def _await_closed(self):
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return
        if self.wsm_close_task is not None:
            await self.wsm_close_task
            self.wsm_close_task = None
        self.wallet_state_manager = None

    def _set_state_changed_callback(self, callback: Callable):
        self.state_changed_callback = callback

        if self.wallet_state_manager is not None:
            self.wallet_state_manager.set_callback(self.state_changed_callback)
            self.wallet_state_manager.set_pending_callback(self._pending_tx_handler)

    def _pending_tx_handler(self):
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return
        asyncio.ensure_future(self._resend_queue())

    async def _action_messages(self) -> List[Message]:
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return []
        actions: List[
            WalletAction
        ] = await self.wallet_state_manager.action_store.get_all_pending_actions()
        result: List[Message] = []
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_generator":
                header_hash = bytes32(hexstr_to_bytes(action_data["header_hash"]))
                height = uint32(action_data["height"])
                msg = Message(
                    "request_generator",
                    wallet_protocol.RequestGenerator(height, header_hash),
                )
                result.append(msg)

        return result

    async def _resend_queue(self):
        if (
            self._shut_down
            or self.server is None
            or self.wallet_state_manager is None
            or self.backup_initialized is None
        ):
            return

        for msg in await self._messages_to_resend():
            if (
                self._shut_down
                or self.server is None
                or self.wallet_state_manager is None
                or self.backup_initialized is None
            ):
                return
            await self.server.send_to_all([msg], NodeType.FULL_NODE)

        for msg in await self._action_messages():
            if (
                self._shut_down
                or self.server is None
                or self.wallet_state_manager is None
                or self.backup_initialized is None
            ):
                return
            await self.server.send_to_all([msg], NodeType.FULL_NODE)

    async def _messages_to_resend(self) -> List[Message]:
        if (
            self.wallet_state_manager is None
            or self.backup_initialized is False
            or self._shut_down
        ):
            return []
        messages: List[Message] = []

        records: List[
            TransactionRecord
        ] = await self.wallet_state_manager.tx_store.get_not_sent()

        for record in records:
            if record.spend_bundle is None:
                continue
            msg = Message(
                "send_transaction",
                wallet_protocol.SendTransaction(record.spend_bundle),
            )
            messages.append(msg)

        return messages

    def _set_server(self, server: ChiaServer):
        self.server = server
        self.wallet_peers = WalletPeers(
            self.server,
            self.root_path,
            self.config["target_peer_count"],
            self.config["wallet_peers_path"],
            self.config["introducer_peer"],
            self.config["peer_connect_interval"],
            self.log,
        )
        asyncio.create_task(self.wallet_peers.start())

    async def _on_connect(self, peer: WSChiaConnection):
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return
        messages = await self._messages_to_resend()
        for msg in messages:
            await peer.send_message(msg)

    async def _periodically_check_full_node(self):
        tries = 0
        while not self._shut_down and tries < 5:
            if self.has_full_node():
                await self.wallet_peers.ensure_is_closed()
                break
            tries += 1
            await asyncio.sleep(180)

    def has_full_node(self) -> bool:
        assert self.server is not None
        if "full_node_peer" in self.config:
            full_node_peer = PeerInfo(
                self.config["full_node_peer"]["host"],
                self.config["full_node_peer"]["port"],
            )
            peers = [c.get_peer_info() for c in self.server.get_full_node_connections()]
            full_node_resolved = PeerInfo(
                socket.gethostbyname(full_node_peer.host), full_node_peer.port
            )
            if full_node_peer in peers or full_node_resolved in peers:
                self.log.info(
                    f"Will not attempt to connect to other nodes, already connected to {full_node_peer}"
                )
                for connection in self.server.get_full_node_connections():
                    if (
                        connection.get_peer_info() != full_node_peer
                        and connection.get_peer_info() != full_node_resolved
                    ):
                        self.log.info(
                            f"Closing unnecessary connection to {connection.get_peer_info()}."
                        )
                    asyncio.ensure_future(connection.close())
                return True
        return False

    async def _sync(self, peer: WSChiaConnection):
        """
        Wallet has fallen far behind (or is starting up for the first time), and must be synced
        up to the LCA of the blockchain.
        """
        if self.server is None:
            return
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return

        # 1. Get all header hashes
        self.header_hashes = []
        self.header_hashes_error = False
        self.proof_hashes = []
        self.potential_header_hashes = {}
        genesis = FullBlock.from_bytes(self.constants.GENESIS_BLOCK)
        genesis_challenge = genesis.proof_of_space.challenge_hash
        request_header_hashes = wallet_protocol.RequestAllHeaderHashesAfter(
            uint32(0), genesis_challenge
        )

        msg = Message("request_all_header_hashes_after", request_header_hashes)
        await peer.send_message(msg)

        timeout = 50
        sleep_interval = 3
        sleep_interval_short = 1
        start_wait = time.time()
        while time.time() - start_wait < timeout:
            if self._shut_down:
                return
            if self.header_hashes_error:
                raise ValueError(
                    f"Received error from full node while fetching hashes from {request_header_hashes}."
                )
            if len(self.header_hashes) > 0:
                break
            await asyncio.sleep(0.5)
        if len(self.header_hashes) == 0:
            raise TimeoutError("Took too long to fetch header hashes.")

        # 2. Find fork point
        fork_point_height: uint32 = (
            self.wallet_state_manager.find_fork_point_alternate_chain(
                self.header_hashes
            )
        )
        fork_point_hash: bytes32 = self.header_hashes[fork_point_height]
        # Sync a little behind, in case there is a short reorg
        tip_height = (
            len(self.header_hashes) - 5
            if len(self.header_hashes) > 5
            else len(self.header_hashes)
        )

        if self.wallet_state_manager.new_wallet is True and fork_point_height == 0:
            self.config["starting_height"] = max(
                0, tip_height - self.config["start_height_buffer"]
            )

        self.log.info(
            f"Fork point: {fork_point_hash} at height {fork_point_height}. Will sync up to {tip_height}"
        )
        for height in range(0, tip_height + 1):
            self.potential_blocks_received[uint32(height)] = asyncio.Event()

        header_validate_start_height: uint32
        if self.config["starting_height"] == 0:
            header_validate_start_height = fork_point_height
        else:
            # Request all proof hashes
            request_proof_hashes = wallet_protocol.RequestAllProofHashes()
            msg = Message("request_all_proof_hashes", request_proof_hashes)
            await peer.send_message(msg)

            start_wait = time.time()
            while time.time() - start_wait < timeout:
                if self._shut_down:
                    return
                if len(self.proof_hashes) > 0:
                    break
                await asyncio.sleep(0.5)
            if len(self.proof_hashes) == 0:
                raise TimeoutError("Took too long to fetch proof hashes.")
            if len(self.proof_hashes) < tip_height:
                raise ValueError("Not enough proof hashes fetched.")

            # Creates map from height to difficulty
            heights: List[uint32] = []
            difficulty_weights: List[uint64] = []
            difficulty: uint64 = uint64(0)
            for i in range(tip_height):
                if self.proof_hashes[i][1] is not None:
                    difficulty = self.proof_hashes[i][1]
                if i > (fork_point_height + 1) and i % 2 == 1:  # Only add odd heights
                    heights.append(uint32(i))
                    difficulty_weights.append(difficulty)

            # Randomly sample based on difficulty
            query_heights_odd = sorted(
                list(
                    set(
                        random.choices(
                            heights, difficulty_weights, k=min(100, len(heights))
                        )
                    )
                )
            )
            query_heights: List[uint32] = []

            for odd_height in query_heights_odd:
                query_heights += [uint32(odd_height - 1), odd_height]

            # Send requests for these heights
            # Verify these proofs
            last_request_time = float(0)
            highest_height_requested = uint32(0)
            request_made = False

            for height_index in range(len(query_heights)):
                total_time_slept = 0
                while True:
                    if self._shut_down:
                        return
                    if total_time_slept > timeout:
                        raise TimeoutError("Took too long to fetch blocks")

                    # Request batches that we don't have yet
                    for batch_start_index in range(
                        height_index,
                        min(
                            height_index + self.config["num_sync_batches"],
                            len(query_heights),
                        ),
                    ):
                        if self._shut_down:
                            return
                        blocks_missing = not self.potential_blocks_received[
                            uint32(query_heights[batch_start_index])
                        ].is_set()
                        if (
                            time.time() - last_request_time > sleep_interval
                            and blocks_missing
                        ) or (
                            query_heights[batch_start_index]
                        ) > highest_height_requested:
                            if (
                                query_heights[batch_start_index]
                                > highest_height_requested
                            ):
                                highest_height_requested = uint32(
                                    query_heights[batch_start_index]
                                )
                            request_made = True
                            request_header = wallet_protocol.RequestHeader(
                                uint32(query_heights[batch_start_index]),
                                self.header_hashes[query_heights[batch_start_index]],
                            )
                            self.log.info(
                                f"Requesting sync header {query_heights[batch_start_index]}"
                            )
                            # TODO send to random
                            await self.server.send_to_all(
                                [Message("request_header", request_header)],
                                NodeType.FULL_NODE,
                            )
                    if request_made:
                        last_request_time = time.time()
                        request_made = False
                    try:
                        aw = self.potential_blocks_received[
                            uint32(query_heights[height_index])
                        ].wait()
                        await asyncio.wait_for(aw, timeout=sleep_interval)
                        break
                    # https://github.com/python/cpython/pull/13528
                    except (concurrent.futures.TimeoutError, asyncio.TimeoutError):
                        total_time_slept += sleep_interval
                        self.log.info("Did not receive desired headers")

            self.log.info(
                f"Finished downloading sample of headers at heights: {query_heights}, validating."
            )
            # Validates the downloaded proofs
            assert self.wallet_state_manager.validate_select_proofs(
                self.proof_hashes,
                query_heights_odd,
                self.cached_blocks,
                self.potential_header_hashes,
            )
            self.log.info("All proofs validated successfuly.")

            # Add blockrecords one at a time, to catch up to starting height
            weight = self.wallet_state_manager.block_records[fork_point_hash].weight

            header_validate_start_height = min(
                max(fork_point_height, self.config["starting_height"] - 1),
                tip_height + 1,
            )
            if fork_point_height == 0:
                difficulty = uint64(self.constants.DIFFICULTY_STARTING)
            else:
                fork_point_parent_hash = self.wallet_state_manager.block_records[
                    fork_point_hash
                ].prev_header_hash
                fork_point_parent_weight = self.wallet_state_manager.block_records[
                    fork_point_parent_hash
                ]
                difficulty = uint64(weight - fork_point_parent_weight.weight)

            for height in range(
                fork_point_height + 1, header_validate_start_height + 1
            ):
                _, difficulty_change, total_iters = self.proof_hashes[height]
                if difficulty_change is not None:
                    difficulty = difficulty_change
                weight = uint128(difficulty + weight)
                block_record = BlockRecord(
                    self.header_hashes[height],
                    self.header_hashes[height - 1],
                    uint32(height),
                    weight,
                    [],
                    [],
                    total_iters,
                    None,
                    uint64(0),
                )
                res = await self.wallet_state_manager.receive_block(block_record, None)
                assert (
                    res == ReceiveBlockResult.ADDED_TO_HEAD
                    or res == ReceiveBlockResult.ADDED_AS_ORPHAN
                )
            self.log.info(
                f"Fast sync successful up to height {header_validate_start_height}"
            )

        # Download headers in batches, and verify them as they come in. We download a few batches ahead,
        # in case there are delays. TODO(mariano): optimize sync by pipelining
        last_request_time = float(0)
        highest_height_requested = uint32(0)
        request_made = False

        for height_checkpoint in range(
            header_validate_start_height + 1, tip_height + 1
        ):
            total_time_slept = 0
            while True:
                # Request batches that we don't have yet
                for batch_start in range(
                    height_checkpoint,
                    min(
                        height_checkpoint + self.config["num_sync_batches"],
                        tip_height + 1,
                    ),
                ):
                    if self._shut_down:
                        return
                    if total_time_slept > timeout:
                        raise TimeoutError("Took too long to fetch blocks")
                    batch_end = min(batch_start + 1, tip_height + 1)
                    blocks_missing = any(
                        [
                            not (self.potential_blocks_received[uint32(h)]).is_set()
                            for h in range(batch_start, batch_end)
                        ]
                    )
                    if (
                        time.time() - last_request_time > sleep_interval
                        and blocks_missing
                    ) or (batch_end - 1) > highest_height_requested:
                        self.log.info(f"Requesting sync header {batch_start}")
                        if batch_end - 1 > highest_height_requested:
                            highest_height_requested = uint32(batch_end - 1)
                        request_made = True
                        request_header = wallet_protocol.RequestHeader(
                            uint32(batch_start),
                            self.header_hashes[batch_start],
                        )

                        # TODO send random
                        msg = Message("request_header", request_header)
                        await self.server.send_to_all([msg], NodeType.FULL_NODE)
                if request_made:
                    last_request_time = time.time()
                    request_made = False

                awaitables = [
                    self.potential_blocks_received[uint32(height_checkpoint)].wait()
                ]
                future = asyncio.gather(*awaitables, return_exceptions=True)
                try:
                    await asyncio.wait_for(future, timeout=sleep_interval)
                # https://github.com/python/cpython/pull/13528
                except (concurrent.futures.TimeoutError, asyncio.TimeoutError):
                    try:
                        await future
                    except asyncio.CancelledError:
                        pass
                    total_time_slept += sleep_interval
                    self.log.info(
                        f"Did not receive desired headers {height_checkpoint}"
                    )
                    continue

                # Succesfully downloaded header. Now confirm it's added to chain.
                hh = self.potential_header_hashes[uint32(height_checkpoint)]
                if hh in self.wallet_state_manager.block_records:
                    # Successfully added the block to chain
                    break
                else:
                    # Not added to chain yet. Try again soon.
                    await asyncio.sleep(sleep_interval_short)
                    if self._shut_down:
                        return
                    total_time_slept += sleep_interval_short
                    if hh in self.wallet_state_manager.block_records:
                        break
                    else:
                        _, hb, tfilter = self.cached_blocks[hh]
                        self.log.warning(
                            f"Received header, but it has not been added to chain. Retrying. {hb.height}"
                        )
                        respond_header_msg = wallet_protocol.RespondHeader(hb, tfilter)
                        await self._respond_header(respond_header_msg, peer)

        self.log.info(
            f"Finished sync process up to height {max(self.wallet_state_manager.height_to_hash.keys())}"
        )

    async def _block_finished(
        self,
        block_record: BlockRecord,
        header_block: HeaderBlock,
        transaction_filter: Optional[bytes],
    ) -> Optional[wallet_protocol.RespondHeader]:
        """
        This is called when we have finished a block (which means we have downloaded the header,
        as well as the relevant additions and removals for the wallets).
        """
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return None
        assert block_record.prev_header_hash in self.wallet_state_manager.block_records
        assert block_record.additions is not None and block_record.removals is not None

        # We have completed a block that we can add to chain, so add it.
        res = await self.wallet_state_manager.receive_block(block_record, header_block)
        if res == ReceiveBlockResult.DISCONNECTED_BLOCK:
            self.log.error("Attempted to add disconnected block")
            return None
        elif res == ReceiveBlockResult.INVALID_BLOCK:
            self.log.error("Attempted to add invalid block")
            return None
        elif res == ReceiveBlockResult.ALREADY_HAVE_BLOCK:
            return None
        elif res == ReceiveBlockResult.ADDED_AS_ORPHAN:
            self.log.info(
                f"Added orphan {block_record.header_hash} at height {block_record.height}"
            )
        elif res == ReceiveBlockResult.ADDED_TO_HEAD:
            self.log.info(
                f"Updated LCA to {block_record.header_hash} at height {block_record.height}"
            )
            # Removes outdated cached blocks if we're not syncing
            if not self.wallet_state_manager.sync_mode:
                remove_header_hashes = []
                for header_hash in self.cached_blocks:
                    if (
                        block_record.height - self.cached_blocks[header_hash][0].height
                        > 100
                    ):
                        remove_header_hashes.append(header_hash)
                for header_hash in remove_header_hashes:
                    del self.cached_blocks[header_hash]
        else:
            raise RuntimeError("Invalid state")

        # Now for the cases of already have, orphan, and added to head, move on to the next block
        if block_record.header_hash in self.future_block_hashes:
            new_hh = self.future_block_hashes[block_record.header_hash]
            _, new_hb, new_tfilter = self.cached_blocks[new_hh]
            return wallet_protocol.RespondHeader(new_hb, new_tfilter)
        return None

    async def _respond_header(
        self, response: wallet_protocol.RespondHeader, peer: WSChiaConnection
    ):
        """
        The full node responds to our RequestHeader call. We cannot finish this block
        until we have the required additions / removals for our wallets.
        """
        if self.wallet_state_manager is None or self.backup_initialized is False:
            return
        while True:
            if self._shut_down:
                return
            # We loop, to avoid infinite recursion. At the end of each iteration, we might want to
            # process the next block, if it exists.

            block = response.header_block

            # If we already have, return
            if block.header_hash in self.wallet_state_manager.block_records:
                return
            if block.height < 1:
                return

            block_record = BlockRecord(
                block.header_hash,
                block.prev_header_hash,
                block.height,
                block.weight,
                None,
                None,
                response.header_block.header.data.total_iters,
                response.header_block.challenge.get_hash(),
                response.header_block.header.data.timestamp,
            )

            if self.wallet_state_manager.sync_mode:
                if uint32(block.height) in self.potential_blocks_received:
                    self.potential_blocks_received[uint32(block.height)].set()
                    self.potential_header_hashes[block.height] = block.header_hash

            # Caches the block so we can finalize it when additions and removals arrive
            self.cached_blocks[block_record.header_hash] = (
                block_record,
                block,
                response.transactions_filter,
            )

            if block.prev_header_hash not in self.wallet_state_manager.block_records:
                # We do not have the previous block record, so wait for that. When the previous gets added to chain,
                # this method will get called again and we can continue. During sync, the previous blocks are already
                # requested. During normal operation, this might not be the case.
                self.future_block_hashes[block.prev_header_hash] = block.header_hash

                lca = self.wallet_state_manager.block_records[
                    self.wallet_state_manager.lca
                ]
                if (
                    block_record.height - lca.height < self.short_sync_threshold
                    and not self.wallet_state_manager.sync_mode
                ):
                    # Only requests the previous block if we are not in sync mode, close to the new block,
                    # and don't have prev
                    header_request = wallet_protocol.RequestHeader(
                        uint32(block_record.height - 1),
                        block_record.prev_header_hash,
                    )

                    msg = Message("request_header", header_request)
                    await peer.send_message(msg)
                return

            # If the block has transactions that we are interested in, fetch adds/deletes
            (
                additions,
                removals,
            ) = await self.wallet_state_manager.get_filter_additions_removals(
                block_record, response.transactions_filter
            )
            if len(additions) > 0 or len(removals) > 0:
                request_a = wallet_protocol.RequestAdditions(
                    block.height, block.header_hash, additions
                )
                msg = Message("request_additions", request_a)
                await peer.send_message(msg)
                return

            # If we don't have any transactions in filter, don't fetch, and finish the block
            block_record = BlockRecord(
                block_record.header_hash,
                block_record.prev_header_hash,
                block_record.height,
                block_record.weight,
                [],
                [],
                block_record.total_iters,
                block_record.new_challenge_hash,
                block_record.timestamp,
            )
            respond_header_msg: Optional[
                wallet_protocol.RespondHeader
            ] = await self._block_finished(
                block_record, block, response.transactions_filter
            )
            if respond_header_msg is None:
                return
            else:
                response = respond_header_msg
