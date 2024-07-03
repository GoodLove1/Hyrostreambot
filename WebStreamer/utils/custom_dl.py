# This file is a part of FileStreamBot

import asyncio
import logging
# import heapq
from typing import Dict, Union
from WebStreamer.bot import work_loads, cdn_count
from hydrogram import Client, utils, raw
from .file_properties import get_file_ids
from hashlib import sha256
from hydrogram.crypto import aes
from hydrogram.session import Session, Auth
from hydrogram.errors import AuthBytesInvalid
from hydrogram.errors import CDNFileHashMismatch
from hydrogram.errors import (
    VolumeLocNotFound, AuthBytesInvalid
)
from hydrogram.file_id import FileId, FileType, ThumbnailSource


class ByteStreamer:
    def __init__(self):
        """A custom class that holds the cache of a specific client and class functions.
        attributes:
            client: the client that the cache is for.
            cached_file_ids: a dict of cached file IDs.
            cached_file_properties: a dict of cached file properties.
        
        functions:
            generate_file_properties: returns the properties for a media of a specific message contained in Tuple.
            generate_media_session: returns the media session for the DC that contains the media file.
            yield_file: yield a file from telegram servers for streaming.
            
        This is a modified version of the <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        self.clean_timer = 30 * 60
        self.cached_file_ids: Dict[str, FileId] = {}
        asyncio.create_task(self.clean_cache())

    async def get_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Returns the properties of a media of a specific message in a FIleId class.
        if the properties are cached, then it'll return the cached results.
        or it'll generate the properties from the Message ID and cache them.
        """
        if not db_id in self.cached_file_ids:
            logging.debug("Before Calling generate_file_properties")
            await self.generate_file_properties(db_id, multi_clients)
            logging.debug(f"Cached file properties for file with ID {db_id}")
        return self.cached_file_ids[db_id]
    
    async def generate_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Generates the properties of a media file on a specific message.
        returns ths properties in a FIleId class.
        """
        logging.debug("Before calling get_file_ids")
        index = min(work_loads, key=work_loads.get)
        # index = heapq.nsmallest(1, work_loads, key=work_loads.get)[0]
        file_id = await get_file_ids(multi_clients[index], db_id, multi_clients, index)
        logging.debug(f"Generated file ID and Unique ID for file with ID {db_id}")
        self.cached_file_ids[db_id] = file_id
        logging.debug(f"Cached media file with ID {db_id}")
        return self.cached_file_ids[db_id]

    async def generate_media_session(self, client: Client, file_id: FileId) -> Session:
        """
        Generates the media session for the DC that contains the media file.
        This is required for getting the bytes from Telegram servers.
        """

        media_session = client.media_sessions.get(file_id.dc_id, None)
        if media_session and not media_session.is_started.is_set():
            logging.info("Removing Disconnected Media Session")
            client.media_sessions.pop(file_id.dc_id, None)
            await media_session.stop()
            media_session=None

        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(
                        client, file_id.dc_id, await client.storage.test_mode()
                    ).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

                for _ in range(6):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                    )

                    try:
                        await media_session.invoke(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logging.debug(
                            f"Invalid authorization bytes for DC {file_id.dc_id}"
                        )
                        continue
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
            logging.debug(f"Created media session for DC {file_id.dc_id}")
            client.media_sessions[file_id.dc_id] = media_session
        else:
            logging.debug(f"Using cached media session for DC {file_id.dc_id}")
        return media_session


    @staticmethod
    async def get_location(file_id: FileId) -> Union[raw.types.InputPhotoFileLocation,
                                                     raw.types.InputDocumentFileLocation,
                                                     raw.types.InputPeerPhotoFileLocation,]:
        """
        Returns the file location for the media file.
        """
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                photo_id=file_id.media_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return location

    async def yield_file(
        self,
        file_id: FileId,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        multi_clients
    ) -> Union[str, None]:
        """
        Custom generator that yields the bytes of the media file.
        Modded from <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py#L20>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        client = multi_clients[file_id.index]
        work_loads[file_id.index] += 1
        logging.debug(f"Starting to yielding file with client {file_id.index}.")
        media_session = await self.generate_media_session(client, file_id)

        current_part = 1

        location = await self.get_location(file_id)

        try:
            r = await media_session.invoke(
                raw.functions.upload.GetFile(
                    location=location, offset=offset, limit=chunk_size
                ),
            )
            if isinstance(r, raw.types.upload.File):
                while True:
                    chunk = r.bytes
                    if not chunk:
                        break
                    elif part_count == 1:
                        yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1:
                        yield chunk[first_part_cut:]
                    elif current_part == part_count:
                        yield chunk[:last_part_cut]
                    else:
                        yield chunk

                    current_part += 1
                    offset += chunk_size

                    if current_part > part_count:
                        break

                    r = await media_session.invoke(
                        raw.functions.upload.GetFile(
                            location=location, offset=offset, limit=chunk_size
                        ),
                    )
            elif isinstance(r, raw.types.upload.FileCdnRedirect):
                cdn_count[file_id.index] = cdn_count[file_id.index]+1 if file_id.index in cdn_count else 1
                cdn_session = Session(
                    client, r.dc_id, await Auth(client, r.dc_id, await client.storage.test_mode()).create(),
                    await client.storage.test_mode(), is_media=True, is_cdn=True
                )

                try:
                    await cdn_session.start()

                    while True:
                        r2 = await cdn_session.invoke(
                            raw.functions.upload.GetCdnFile(
                                file_token=r.file_token,
                                offset=offset,
                                limit=chunk_size
                            )
                        )

                        if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                            try:
                                await media_session.invoke(
                                    raw.functions.upload.ReuploadCdnFile(
                                        file_token=r.file_token,
                                        request_token=r2.request_token
                                    )
                                )
                            except VolumeLocNotFound:
                                break
                            else:
                                continue

                        chunk = r2.bytes

                        # https://core.telegram.org/cdn#decrypting-files
                        decrypted_chunk = aes.ctr256_decrypt(
                            chunk,
                            r.encryption_key,
                            bytearray(
                                r.encryption_iv[:-4]
                                + (offset // 16).to_bytes(4, "big")
                            )
                        )

                        hashes = await media_session.invoke(
                            raw.functions.upload.GetCdnFileHashes(
                                file_token=r.file_token,
                                offset=offset
                            )
                        )

                        # https://core.telegram.org/cdn#verifying-files
                        for i, h in enumerate(hashes):
                            cdn_chunk = decrypted_chunk[h.limit * i: h.limit * (i + 1)]
                            CDNFileHashMismatch.check(
                                h.hash == sha256(cdn_chunk).digest(),
                                "h.hash == sha256(cdn_chunk).digest()"
                            )

                        if not decrypted_chunk:
                            break
                        elif part_count == 1:
                            yield decrypted_chunk[first_part_cut:last_part_cut]
                        elif current_part == 1:
                            yield decrypted_chunk[first_part_cut:]
                        elif current_part == part_count:
                            yield decrypted_chunk[:last_part_cut]
                        else:
                            yield decrypted_chunk

                        current_part += 1
                        offset += chunk_size

                        if current_part > part_count:
                            break
                except Exception as e:
                    raise e
                finally:
                    await cdn_session.stop()
        except (TimeoutError, AttributeError):
            pass
        except OSError as e:
            logging.info("Removing Media Session")
            client.media_sessions.pop(file_id.dc_id, None)
            await media_session.stop()
            logging.error(e)
            raise e
        finally:
            logging.debug(f"Finished yielding file with {current_part} parts.")
            work_loads[file_id.index] -= 1

    
    async def clean_cache(self) -> None:
        """
        function to clean the cache to reduce memory usage
        """
        while True:
            await asyncio.sleep(self.clean_timer)
            self.cached_file_ids.clear()
            logging.debug("Cleaned the cache")
