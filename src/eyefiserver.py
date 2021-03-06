#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# This is a standalone Eye-Fi Server
#
#  Copyright (c) 2009 Jeffrey Tchang
#  Copyright (c) 2010 Pieter van Kemenade
#  Copyright (c) 2011 Jeremy Fitzhardinge
#  Copyright (c) 2011 Grant Nakamura
#  Copyright (c) 2012 Jean-Michel Nirgal Vourgère
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

"""
This is a standalone Eye-Fi Server that is designed to take the place of the
Eye-Fi Manager.

Starting this server creates a listener on port 59278. I use the BaseHTTPServer
class included with Python. I look for specific POST/GET request URLs and
execute functions based on those URLs.
"""

import sys
import os
import select
import subprocess
import signal
try:
    import daemon
except:
    print('This tool requires the python-daemon package!')
    sys.exit(1)
from StringIO import StringIO
import hashlib
import binascii
import struct
import array
import tarfile
from datetime import datetime, timedelta
from ConfigParser import RawConfigParser, NoSectionError, NoOptionError
import re
try:
    import argparse
except:
    print('This tool requires python-argparse to function!')
    sys.exit(1)
import cgi
import logging
import logging.handlers
import socket
import random
import xml.sax
import xml.dom.minidom
from BaseHTTPServer import BaseHTTPRequestHandler
import BaseHTTPServer
import SocketServer

# Default is to listen to all addresses on port 59278
# Exemple: SERVER_ADDRESS = '127.0.0.1', 59278
SERVER_ADDRESS = '', 59278

# How many bytes are read at once:
# If the value is too small, non-upload request might fail
# If the value is too big, the progress meter will not be precise
READ_CHUNK_SIZE = 10 * 1024

# Repport download progress every few seconds
PROGRESS_FREQUENCY = timedelta(0, 1)

# The server HTTP header
HTTP_SERVER_NAME = 'Eye-Fi Agent/2.0.4.0 (Windows XP SP2)'

# Format of log messages:
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'

# KNOW BUGS:
# logger doesn't catch exception from do_POST threads and such.
# So these errors are logged to stderr only, not in log files.
# Prefer stderr for debugging

# Create the main logger
eyeFiLogger = logging.Logger("eyeFiLogger", logging.DEBUG)

class IntegrityDigestFile(file):
    """
    Wrapper around file object
    All write() calls are used to compute a CRC the Eye-Fi way
    """
    def __init__(self, name, mode='r'):
        file.__init__(self, name, mode )
        # Create an array of 2 byte integers
        self.concatenated_tcp_checksums = array.array('H')
        self.todo_buffer = '' # bytes that weren't used yet

    def seek(self, position, whence=0):
        assert whence == 0, \
            "IntegrityDigestFile does not support seek(whence!=0)"
        self.concatenated_tcp_checksums = array.array('H')
        self.todo_buffer = ''
        file.seek(self, 0)
        lefttoread = position
        while True:
            if lefttoread == 0:
                return
            readsize = min(512, lefttoread)
            buf = self.read(readsize)
            assert len(buf) == readsize, \
                "Failed to read %s bytes" % readsize
            self._diggestpush(buf)
            lefttoread -= readsize

    @staticmethod
    def calculate_tcp_checksum(buf):
        """
        The TCP checksum requires an even number of bytes. If an even
        number of bytes is not passed in then nul pad the input and then
        compute the checksum
        """

        # If the number of bytes I was given is not a multiple of 2
        # pad the input with a null character at the end
        if len(buf) % 2 != 0:
            buf = buf + "\x00"

        sum_of_shorts = 0

        # For each pair of bytes, cast them into a 2 byte integer (unsigned
        # short).
        # Compute using little-endian (which is what the '<' sign if for)
        for ushort in struct.unpack('<' + 'H' * (len(buf)/2), buf):
            # Add them all up
            sum_of_shorts = sum_of_shorts + int(ushort)

        # The sum at this point is probably a 32 bit integer. Take the left 16
        # bits and the right 16 bites, interpret both as an integer of max value
        # 2^16 and add them together. If the resulting value is still bigger
        # than 2^16 then do it again until we get a value less than 16 bits.
        while sum_of_shorts >> 16:
            sum_of_shorts = (sum_of_shorts >> 16) + (sum_of_shorts & 0xFFFF)

        # Take the one's complement of the result through the use of an xor
        checksum = sum_of_shorts ^ 0xFFFFFFFF

        # Compute the final checksum by taking only the last 16 bits
        checksum = checksum & 0xFFFF

        return checksum

    def _diggestpush(self, buf):
        """
        Push some bytes in the digest processing mechanism
        """
        self.todo_buffer += buf
        while (len(self.todo_buffer) > 512):
            tcp_checksum = self.calculate_tcp_checksum(self.todo_buffer[:512])
            self.concatenated_tcp_checksums.append(tcp_checksum)
            self.todo_buffer = self.todo_buffer[512:]

    def write(self, buf):
        file.write(self, buf)
        self._diggestpush(buf)

    def getintegritydigest(self, uploadkey):
        """
        Returns Eye-Fi CRC value based on previous write()
        """
        # send the remaining buffer
        tcp_checksum = self.calculate_tcp_checksum(self.todo_buffer)
        self.concatenated_tcp_checksums.append(tcp_checksum)

        # Append the upload key
        binuploadkey = binascii.unhexlify(uploadkey)
        self.concatenated_tcp_checksums.fromstring(binuploadkey)

        # Get the concatenated_tcp_checksums array as a binary string
        integritydigest = self.concatenated_tcp_checksums.tostring()

        # MD5 hash the binary string
        md5 = hashlib.md5()
        md5.update(integritydigest)

        # Hex encode the hash to obtain the final integrity digest
        return md5.hexdigest()

class EyeFiContentHandler(xml.sax.handler.ContentHandler):
    "Eye Fi XML SAX ContentHandler"

    def __init__(self):
        xml.sax.handler.ContentHandler.__init__(self)
        self.extracted_elements = {} # Where to put the extracted values
        self.last_element_name = ''

    def startElement(self, name, attributes):
        self.last_element_name = name

    def characters(self, content):
        self.extracted_elements[self.last_element_name] = content

class EyeFiServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    "Implements an EyeFi server"

    def get_request(self):
        connection, address = BaseHTTPServer.HTTPServer.get_request(self)
        eyeFiLogger.debug("Incoming request from client %s", address)
        # It is important to have a non-null timeout because the card will send
        # empty server discovery packets: These are never closed in a proper
        # way, and would stack forever on the server side.
        # Note that these discover packets all comes from socketnumber ~ 8000
        # or 9000. Maybe we should discard them, but then we risk to drop valid
        # connection atempts.
        connection.settimeout(15)
        return connection, address

def build_soap_response(actionname, items):
    """
    Build an SOAP response in EyeFi format:
    actionname is a simple string such as GetPhotoStatusResponse
    items is a list of tupple (key, value)
    """
    # Create the XML document to send back
    doc = xml.dom.minidom.Document()

    soapenv_element = doc.createElementNS(
        'http://schemas.xmlsoap.org/soap/envelope/',
        'SOAP-ENV:Envelope')
    soapenv_element.setAttribute(
        'xmlns:SOAP-ENV',
        'http://schemas.xmlsoap.org/soap/envelope/')
    doc.appendChild(soapenv_element)

    soapbody_element = doc.createElement("SOAP-ENV:Body")
    soapenv_element.appendChild(soapbody_element)

    soapaction_element = doc.createElement(actionname)
    soapaction_element.setAttribute(
        'xmlns',
        'http://localhost/api/soap/eyefilm')
    soapbody_element.appendChild(soapaction_element)

    for key, value in items:
        item_element = doc.createElement(key)
        soapaction_element.appendChild(item_element)

        item_elementtext = doc.createTextNode(str(value))
        item_element.appendChild(item_elementtext)
    return doc.toxml(encoding="UTF-8")

class EyeFiSession:
    """
    Contains data for an HTTP session
    """
    def __init__(self, macaddress, cnonce):
        self.macaddress = macaddress
        self.cnonce = cnonce
        self.snonce = self._randomnonce()
        self.filesignature = None
        # If a thread is about to time out, some data might not be writen yet
        # If another thread is picking up a resume transfer, we need to
        # remember where to start.
        self.fileoffset = 0

    @staticmethod
    def _randomnonce():
        """
        Return a random 32 digit hexadecimal value as a string
        """
        result = ''
        for ite in range(8):
            result += "%04x" % (0xffff * random.random())
        return result

    @staticmethod
    def _hexmd5(plain):
        """
        returns md5(plain) with plain and return values as hexadecimal
        representations
        """
        # Return the binary data represented by the hexadecimal string
        # resulting in something that looks like "\x00\x18V\x03\x04..."
        binplain = binascii.unhexlify(plain)

        # Now MD5 hash the binary string
        md5 = hashlib.md5()
        md5.update(binplain)

        # Hex encode the hash to obtain the final credential string
        return md5.hexdigest()

    def getclientcredential(self, config):
        """
        This is the StartSessionResponse challenge:
        returns md5(macaddress+cnonce+upload_key)
        """
        upload_key = config.get_key(self.macaddress, 'upload_key')
        eyeFiLogger.debug("Setting Eye-Fi upload key to %s", upload_key)

        credentialstring = self.macaddress + self.cnonce + upload_key
        eyeFiLogger.debug("Concatenated credential string (pre MD5): %s",
                          credentialstring)

        return self._hexmd5(credentialstring)

    def getservercredential(self, config):
        """
        This is the GetPhotoStatus challenge:
        returns md5(macaddress+upload_key+snonce)
        """
        upload_key = config.get_key(self.macaddress, 'upload_key')
        eyeFiLogger.debug("Setting Eye-Fi upload key to %s", upload_key)

        credentialstring = self.macaddress + upload_key + self.snonce
        eyeFiLogger.debug("Concatenated credential string (pre MD5): %s",
                          credentialstring)

        return self._hexmd5(credentialstring)

class EyeFiRequestHandler(BaseHTTPRequestHandler):
    """This class is responsible for handling HTTP requests passed to it.
    It implements the common HTTP method do_POST()"""

    def __init__(self, *args, **kargs):
        # We need a way to force self.close_connection:
        self.eyefi_close_connection = False
        self.session = None
        # For some obscure reason, BaseHTTPRequestHandler.__init__ need to be
        # called last:
        BaseHTTPRequestHandler.__init__(self, *args, **kargs)

    def split_multipart(self, postdata):
        """
        Takes a EyeFi http posted data
        Returns a dictionnary of multipart/form-data if available
        Otherwise returns returns a dictionary with a single key 'SOAPENVELOPE'
        """
        content_type = self.headers.get('content-type', '')
        if content_type.startswith('multipart/form-data'):
            # content-type header looks something like this
            # multipart/form-data; boundary=---------------------------02468a...
            multipart_boundary = content_type.split('=')[1].strip()

            form = cgi.parse_multipart(StringIO(postdata),
                {'boundary': multipart_boundary})
            eyeFiLogger.debug("Available multipart/form-data: %s", form.keys())

            # Keep only the first value for each key
            for key in form.keys():
                form[key] = form[key][0]
            return form
        else:
            return {'SOAPENVELOPE': postdata}

    def do_POST(self):
        """
        That function is called when a HTTP POST request is received.
        """
        # Be somewhat nicer after a real connection has been achieved
        # see EyeFiServer.get_request comments
        self.connection.settimeout(60)

        # Debug dump request:
        eyeFiLogger.debug("%s %s %s",
                          self.command, self.path, self.request_version)

        eyeFiLogger.debug("Headers received in POST request:")
        for name, value in self.headers.items():
            eyeFiLogger.debug(name + ": " + value)

        # Read at most READ_CHUNK_SIZE bytes of POST data
        content_length = int(self.headers.get("content-length"))
        readsize = min(content_length, READ_CHUNK_SIZE)
        eyeFiLogger.debug("Reading %d bytes of data", readsize)
        postdata = self.rfile.read(readsize)
        if len(postdata) != readsize:
            eyeFiLogger.error('Failed to read %s bytes', readsize)
            self.send_eyefi_response(None)
            return

        splited_postdata = self.split_multipart(postdata)

        soapenv = splited_postdata['SOAPENVELOPE']

        # Delegating the XML parsing of postdata to EyeFiContentHandler()
        handler = EyeFiContentHandler()
        xml.sax.parseString(soapenv, handler)
        soapdata = handler.extracted_elements

        # Perform action based on path and soapaction
        if self.path == "/api/soap/eyefilm/v1":
            eyeFiLogger.debug("%s", postdata)

            # Get and normalize soapaction http header
            soapaction = self.headers.get("soapaction", "")
            if soapaction[:5] == '"urn:' and soapaction[-1] == '"':
                soapaction = soapaction[5:-1]
            else:
                eyeFiLogger.error('soapaction should have format "urn:action"')
                self.send_eyefi_response(None)
                return

            eyeFiLogger.info("Got request %s(%s)", soapaction, ", ".join(
                    ["%s='%s'" % (key, value)
                     for key, value in soapdata.items()]))

            if soapaction == 'StartSession':
                # A soapaction of StartSession indicates the beginning of an
                # EyeFi authentication request
                response = self.startSession(soapdata)

            elif soapaction == 'GetPhotoStatus':
                # GetPhotoStatus allows the card to query if a photo has been
                # uploaded to the server yet
                response = self.getPhotoStatus(soapdata)

            elif soapaction == 'MarkLastPhotoInRoll':
                # If soapaction is MarkLastPhotoInRoll
                response = self.markLastPhotoInRoll(soapdata)

            else:
                eyeFiLogger.error('Unsupported soap action %s', soapaction)
                self.send_eyefi_response(None)
                return

            eyeFiLogger.debug("%s response: %s", soapaction, response)

        elif self.path == "/api/soap/eyefilm/v1/upload":
            # If the URL is upload, the card is ready to send a picture to me

            eyeFiLogger.info("Got request UploadPhoto(%s)", ", ".join(
                             ["%s='%s'" % (key, value)
                              for key, value in soapdata.items()]))

            tardata = splited_postdata['FILENAME'] # just the begining

            response = self.uploadPhoto(postdata, soapdata, tardata)
            eyeFiLogger.debug("Upload response: %s", response)

        else:
            eyeFiLogger.error('Unsupported POST request: url="%s"', self.path)
            self.send_eyefi_response(None)
            return

        self.send_eyefi_response(response)

    def send_eyefi_response(self, response):
        """
        Sends the response text to the connection in HTTP.
        Close the connection if needed.
        """
        if response is None:
            self.send_response(500)
            eyeFiLogger.info('Sending HTTP 500 error code')
        else:
            self.send_response(200)
        self.send_header('Date', self.date_time_string())
        self.send_header('Pragma', 'no-cache')
        self.send_header('Server', HTTP_SERVER_NAME)
        if response is None:
            self.send_header('Connection', 'Close')
            return

        self.send_header('Content-Type', 'text/xml; charset="utf-8"')
        self.send_header('Content-Length', len(response))
        if self.headers.get('Connection', '') == 'Keep-Alive' \
         and response is not None \
         and not self.eyefi_close_connection:
            self.send_header('Connection', 'Keep-Alive')
            eyeFiLogger.debug('Keeping connection alive')
        else:
            self.send_header('Connection', 'Close')
            eyeFiLogger.debug('Closing connection')
        self.end_headers()

        if response is not None:
            self.wfile.write(response)
        self.wfile.flush()

    def markLastPhotoInRoll(self, soapdata):
        "Handles MarkLastPhotoInRoll action"

        return build_soap_response('MarkLastPhotoInRollResponse', [])

    def uploadPhoto(self, postdata_fragment, soapdata, tardata):
        """
        Handles receiving the actual photograph from the card.
        postdata_fragment will most likely contain multipart binary post data that needs to
        be parsed.
        """
        # Here, tardata is only the first bytes of tar file content

        def uploadphoto_response(success):
            """
            Helper function
            """
            return build_soap_response('UploadPhotoResponse', [
                ('success', success),
                ])

        macaddress = soapdata["macaddress"]

        # Check/create upload_tmpdir
        upload_tmpdir = self.server.config.getuploaddir(macaddress)
        if not os.path.isdir(upload_tmpdir):
            try:
                os.makedirs(upload_tmpdir)
            except OSError as err:
                # This is what you get when upload_dir is not writable.
                eyeFiLogger.critical("Can't create directory. %s. "
                                + "Do check write permissions.",
                    err)
                self.eyefi_close_connection = 1
                return uploadphoto_response('false')
            eyeFiLogger.debug("Generated path %s", upload_tmpdir)
            #if uid!=0 and gid!=0:
            #    os.chown(upload_tmpdir, uid, gid)
            #if mode!="":
            #    os.chmod(upload_tmpdir, string.atoi(mode))

        tarpath = os.path.join(upload_tmpdir, self.session.filesignature)
        if os.path.exists(tarpath):
            writemode = 'r+b'
        else:
            writemode = 'wb'
        try:
            tarfilehandle = IntegrityDigestFile(tarpath, writemode)
        except IOError as err:
            # This is what you get when upload_dir is not writable.
            eyeFiLogger.critical("Can't open file %s mode %s: %s. "
                            + "Do check write permissions.",
                tarpath, writemode, err)
            self.eyefi_close_connection = 1
            return uploadphoto_response('false')
        #if uid!=0 and gid!=0:
        #    os.chown(tarpath, uid, gid)
        #if mode!="":
        #    os.chmod(tarpath, string.atoi(mode))

        eyeFiLogger.debug("Opened file %s for binary writing", tarpath)

        tarfilehandle.seek(self.session.fileoffset) # seek in the file
        tarsize = self.session.fileoffset # size allready there
        tarfinalsize = int(soapdata['filesize']) # size to reach

        tarfilehandle.write(tardata)
        tarsize += len(tardata)

        # Read remaining POST data
        received_length = len(postdata_fragment) # size already read
        content_length = int(self.headers.get("content-length"))
        speedtest_starttime = datetime.utcnow()
        speedtest_startsize = received_length
        while received_length < content_length:
            readsize = min(content_length - received_length, READ_CHUNK_SIZE)

            try:
                readdata = self.rfile.read(readsize)
            except socket.timeout:
                readdata = ''
                eyeFiLogger.error('Timout while reading socket')

            # We need to keep the last received data for integrity verification
            postdata_fragment += readdata
            # keep only the last 2kB
            postdata_fragment = postdata_fragment[-2048:]
            received_length += len(readdata)

            if tarsize < tarfinalsize:
                if tarsize + len(readdata) <= tarfinalsize:
                    tarfilehandle.write(readdata)
                    tarsize += len(readdata)
                else:
                    tarfilehandle.write(readdata[:tarfinalsize-tarsize])
                    tarsize = tarfinalsize

            if len(readdata) != readsize:
                eyeFiLogger.error('Failed to read %s bytes', readsize)
                self.eyefi_close_connection = 1
                return uploadphoto_response('false')
            try:
                isshutdown = self.server._BaseServer__shutdown_request
            except AttributeError:
                isshutdown = False
            if isshutdown:
                eyeFiLogger.error('Aborting file reception on shutdown request')
                self.eyefi_close_connection = 1
                return uploadphoto_response('false')

            if datetime.utcnow() - speedtest_starttime > PROGRESS_FREQUENCY:
                elapsed_time = datetime.utcnow() - speedtest_starttime

                elapsed_seconds = elapsed_time.days * 86400 \
                                + elapsed_time.seconds \
                                + elapsed_time.microseconds / 1000000.

                speed = (received_length-speedtest_startsize)/elapsed_seconds*8
                eyeFiLogger.debug("%s: Read %s / %s bytes (%02.02f%%) %d kbps",
                    soapdata['filename'],
                    received_length,
                    content_length,
                    received_length * 100. / content_length,
                    speed/1000
                    )

                speedtest_starttime = datetime.utcnow()
                speedtest_startsize = received_length

                # Run a command on the file if specified
                execute_cmd = self.server.config.get_key(macaddress,
                                                     'progress_execute', '')
                if execute_cmd:
                    execute_cmd = [execute_cmd,
                                   tarpath,
                                   str(tarfinalsize),
                                   str(speed)]
                    eyeFiLogger.debug('Executing command "%s"',
                                      ' '.join(execute_cmd))
                    subprocess.Popen(execute_cmd)

        #pike
        #uid = self.server.config.getint('EyeFiServer', 'upload_uid')
        #gid = self.server.config.getint('EyeFiServer', 'upload_gid')
        #mode = self.server.config.get_key('EyeFiServer', 'upload_mode')
        #eyeFiLogger.debug("Using uid/gid %d/%d"%(uid, gid))
        #eyeFiLogger.debug("Using mode %s", mode)

        tarfilehandle.close()
        eyeFiLogger.debug("Closed file %s", tarpath)


        integrity_verification = self.server.config.getboolean(
                macaddress, 'integrity_verification', True)

        if integrity_verification:
            # Parse postdata_fragment to get INTEGRITYDIGEST key.
            splited_postdata = self.split_multipart(postdata_fragment)

            upload_key = self.server.config.get_key(macaddress, 'upload_key')

            # Perform an integrity check on the file
            verified_digest = tarfilehandle.getintegritydigest(upload_key)
            try:
                unverified_digest = splited_postdata['INTEGRITYDIGEST']
            except KeyError:
                eyeFiLogger.error("No INTEGRITYDIGEST received.")
            else:
                eyeFiLogger.debug(
                    "Comparing my digest [%s] to card's digest [%s].",
                    verified_digest, unverified_digest)
                if verified_digest == unverified_digest:
                    eyeFiLogger.debug("INTEGRITYDIGEST passes test.")
                else:
                    eyeFiLogger.error(
                        "INTEGRITYDIGEST pass failed. File rejected.")
                    eyeFiLogger.debug("Deleting TAR file %s", tarpath)
                    os.remove(tarpath)
                    return uploadphoto_response('false')

        imagetarfile = tarfile.open(tarpath)

        # Get date_from_file flag
        use_date_from_file = self.server.config.get_key(macaddress,
            'use_date_from_file', False)

        # if needed, get reference date from the tar fragment
        # This is possible because the tar content is at the begining
        if use_date_from_file:
            tarinfo = imagetarfile.getmembers()[0]
            imageinfo = tarinfo.get_info(encoding=None, errors=None)
            reference_date = datetime.fromtimestamp(imageinfo['mtime'])
        else:
            reference_date = datetime.now()

        upload_dir = self.server.config.getuploaddir(macaddress, reference_date)

        # Check/create upload_dir
        if not os.path.isdir(upload_dir):
            os.makedirs(upload_dir)
            eyeFiLogger.debug("Generated path %s", upload_dir)
            #if uid!=0 and gid!=0:
            #    os.chown(upload_dir, uid, gid)
            #if mode!="":
            #    os.chmod(upload_dir, string.atoi(mode))

        eyeFiLogger.debug("Extracting TAR file %s", tarpath)
        imagefilename = imagetarfile.getnames()[0]
        imagetarfile.extractall(path=upload_dir)

        eyeFiLogger.debug("Closing TAR file %s", tarpath)
        imagetarfile.close()

        eyeFiLogger.debug("Deleting TAR file %s", tarpath)
        os.remove(tarpath)

        # Run a command on the file if specified
        execute_cmd = self.server.config.get_key(macaddress, 'complete_execute', '')
        if execute_cmd:
            imagepath = os.path.join(upload_dir, imagefilename)
            eyeFiLogger.debug('Executing command "%s %s"',
                              execute_cmd, imagepath)
            subprocess.Popen([execute_cmd, imagepath])

        return uploadphoto_response('true')

    def getPhotoStatus(self, soapdata):
        "Handles GetPhotoStatus action"

        macaddress = soapdata['macaddress']
        if self.session.macaddress != macaddress:
            eyeFiLogger.error("GetPhotoStatus uses a macaddress [%s] " \
               "different that session's one [%]",
               macaddress, self.session.macaddress)
            self.eyefi_close_connection = 1
            return None

        credential = self.session.getservercredential(self.server.config)
        if soapdata['credential'] != credential:
            eyeFiLogger.error('GetPhotoStatus authentication failure:' \
                "Received credential [%s] != [%s]",
                soapdata['credential'], credential)
            self.eyefi_close_connection = 1
            return None

        # Get upload_dir
        upload_tmpdir = self.server.config.getuploaddir(macaddress)

        self.session.filesignature = soapdata['filesignature']
        tarpath = os.path.join(upload_tmpdir, soapdata['filesignature'])

        eyeFiLogger.debug('Checking for partial file %s', tarpath)
        try:
            offset = os.path.getsize(tarpath)
        except OSError:
            offset = 0

        # This is needed for integrity verification:
        offset = offset - offset % 512 # Make align on a 512B block

        self.session.fileoffset = offset

        return build_soap_response('GetPhotoStatusResponse', [
            ('fileid', '1'),
            ('offset', offset), # How many bytes have already been transfered
            ])

    def startSession(self, soapdata):
        "Handle startSession requests"
        macaddress = soapdata['macaddress']
        cnonce = soapdata['cnonce']

        if self.session is not None:
            eyeFiLogger.warning('Overwriting existing session')
        self.session = EyeFiSession(macaddress, cnonce)

        credential = self.session.getclientcredential(self.server.config)

        return build_soap_response('StartSessionResponse', [
            ('credential', credential),
            ('snonce', self.session.snonce),
            ('transfermode', soapdata['transfermode']),
            ('transfermodetimestamp', soapdata['transfermodetimestamp']),
            ('upsyncallowed', 'false'),
            ])

class EyeFiConfig(RawConfigParser):
    """
    Helper wraper around ConfigParser.RawConfigParser
    Provides get() method with fallback to a global section and default values
    """
    def __init__(self, conffiles):
        RawConfigParser.__init__(self)
        self.read(conffiles) # immediatly read files
        self.setloglevel() # set log level
        eyeFiLogger.info("Read config from %s", conffiles)

    def get_key(self, macaddress, option, default=None):
        if macaddress:
            try:
                return self.get(macaddress, option)
            except (NoSectionError, NoOptionError):
                pass
        try:
            return self.get('EyeFiServer', option)
        except (NoSectionError, NoOptionError):
            if default is None:
                eyeFiLogger.error('You need to define key %s' \
                    ' in your config file. See eyefiserver.conf(5)', option)
                raise StandardError('Undefined key in eyefiserver.conf: %s' % option)
        return default

    def getboolean(self, section, option, default=None):
        val = self.get_key(section, option, default)
        if isinstance(val, bool):
            return val
        if val.lower() not in self._boolean_states:
            eyeFiLogger.error('Not a boolean: %s' % val)
            raise ValueError, 'Not a boolean: %s' % val
        return self._boolean_states[val.lower()]

    def setloglevel(self):
        """
        Set global loglevel based on configuration
        """
        # (re)set logger verbosity level
        loglevel = self.get_key(None, 'loglevel', 'DEBUG')
        if loglevel not in ['DEBUG', 'INFO', 'WARN', 'WARNING', 'ERROR', 'FATAL']:
            eyeFiLogger.error('Cannot set new log level %s' % loglevel)
            raise StandardError('Error in conf file: Invalid loglevel')
        loglevel = eval('logging.'+loglevel)
        eyeFiLogger.setLevel(loglevel)

    def getuploaddir(self, macaddress, reference_date=None):
        """
        Returns the upload directory from the configuration file
        It can be card specific base on macaddress
        ~ are expanded to $HOME
        If refenrence_date is provided, resolved %Y and such, see strftime
        If refenrence_date is not provided, %Y and such are removed, so that
        the result is suitable for temporary files.
        """
        upload_dir = self.get_key(macaddress, 'upload_dir', '~/eyefi')
        upload_dir = os.path.expanduser(upload_dir) # expands ~

        if reference_date is None:
            # Remove all the variable part from the dir, and use this as temp
            # location
            upload_tmpdir = re.sub('%.', '', upload_dir)
            # Remove duplicate os.path.sep:
            regexp = os.path.sep.replace('\\', '\\\\') + '+'
            upload_tmpdir = re.sub(regexp, os.path.sep, upload_tmpdir)
            return upload_tmpdir
        else:
            # resolves %Y and so inside upload_dir value
            upload_dir = reference_date.strftime(upload_dir)
            return upload_dir

def main(options):
    """
    Main function
    """
    # Create two handlers. One to print to the log and one to print to the
    # console based on the effective level of logging
    loggingformater = logging.Formatter(LOG_FORMAT)
    if not options.daemon:
        consolehandler = logging.StreamHandler(sys.stderr)
        # Set how both handlers will print the pretty log events
        consolehandler.setFormatter(loggingformater)
        # Append both handlers to the main Eye Fi Server logger
        eyeFiLogger.addHandler(consolehandler)
    else:
        with open(options.pidfile, 'w') as f_out:
            eyeFiLogger.info('Creating pid file at %s' % options.pidfile)
            f_out.write('%d' % os.getpid())
    # open file logging
    if options.logfile:
        hdlr = logging.handlers.RotatingFileHandler(options.logfile, maxBytes=2000000, backupCount=2)
        hdlr.setFormatter(loggingformater)
        eyeFiLogger.addHandler(hdlr)
    
    # run webserver as www-data - cant get it working
    #if config.get('EyeFiServer', 'user_id')!='':
    #    os.setuid(config.getint('EyeFiServer', 'user_id'))
    try:
	# Create an instance of an HTTP server. Requests will be handled
	# by the class EyeFiRequestHandler
	eyefiserver = EyeFiServer(SERVER_ADDRESS, EyeFiRequestHandler)
	eyefiserver.config = EyeFiConfig(options.conffiles)
	eyeFiLogger.info("Eye-Fi server starts listening on port %s",
			 SERVER_ADDRESS[1])
        eyeFiLogger.info('Attempting to use %s for upload_key' % eyefiserver.config.get_key(None, 'upload_key'))
	while True:
	    try:
		eyefiserver.serve_forever()
	    except select.error as err:
		if err.args[0] == 4: # system call interrupted by SIGHUP
		    pass # ignore it
		else:
		    raise
    except KeyboardInterrupt:
	eyeFiLogger.info("Eye-Fi server shutting down")
	eyefiserver.socket.close()
	eyefiserver.shutdown()
	eyeFiLogger.info("Waiting for threads to finish.")
    hdlr.close()

def cli_opts():
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf', '-c',
        action='append', dest='conffiles', metavar='CONFFILE',
        help='specific alternate location for configuration file.',
        default=['/etc/eyefiserver.conf',
                 os.path.expanduser('~/eyefiserver.conf'),
                 '%s/eyefiserver.conf' % os.getcwd(),
                 '%s/eyefiserver.conf' % sys.argv[0].rsplit('/', 1)[0]])
    parser.add_argument('--log', '-l', dest='logfile',
        help='log to file')
    parser.add_argument('--daemon', '-d', action='store_true',
        dest='daemon', help='Daemonize!', default=False)
    parser.add_argument('--pidfile', '-p', dest='pidfile',
        help='pid file location', default='/var/run/eyefiserver.pid')
    return parser.parse_args()

if __name__ == '__main__':
    options = cli_opts()

    if options.daemon:
        context = daemon.DaemonContext(working_directory=os.getcwd(), uid=os.getuid())
        with context:
            main(options)
    else:
        main(options)
# ex: expandtab tabstop=4 syntax=python
