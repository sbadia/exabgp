# encoding: utf-8
"""
network.py

Created by Thomas Mangin on 2009-09-06.
Copyright (c) 2009-2013 Exa Networks. All rights reserved.
"""

import time
import socket
import select
from struct import unpack

from exabgp.configuration.environment import environment

from exabgp.util.od import od
from exabgp.util.trace import trace
from exabgp.util.coroutine import each

from exabgp.logger import Logger,FakeLogger,LazyFormat

from exabgp.bgp.message import Message

from exabgp.reactor.network.error import error,errno,NetworkError,TooSlowError,NotConnected,LostConnection

from .error import *

def strerrno (e):
	return '[errno %s], %s' % (errno.errorcode[e.args[0]],str(e))

class Connection (object):
	def __init__ (self,afi,peer,local):
		# peer and local are strings of the IP

		# If the OS tells us we have data on the socket, we should never have to wait more than read_timeout to be able to read it.
		# However real life says that on some OS we do ... So let the user control this value
		try:
			self.read_timeout = environment.settings().tcp.timeout
			self.logger = Logger()
		except RuntimeError:
			self.logger = FakeLogger
			self.read_timeout = 1
			self.logger = FakeLogger()

		self.afi = afi
		self.peer = peer
		self.local = local

		self._reading = None
		self._writing = None
		self._buffer = ''

	def close (self):
		try:
			self.logger.wire("Closing connection to %s" % self.peer)
			if self.io:
				self.io.close()
				self.io = None
		except KeyboardInterrupt,e:
			raise e
		except:
			pass

	def reading (self):
		while True:
			if self._reading and time.time() > self._reading + self.read_timeout:
				self.close()
				self.logger.wire("%15s peer is too slow" % self.peer)
				raise TooSlowError('Waited for data on a socket for more than %d second(s)' % self.read_timeout)

			try:
				r,_,_ = select.select([self.io,],[],[],0)
			except select.error,e:
				if e.args[0] not in error.block:
					self.close()
					self.logger.wire("%15s errno %s on socket" % (self.peer,errno.errorcode[e.args[0]]))
					raise NetworkError('errno %s on socket' % errno.errorcode[e.args[0]])
				return False

			if r:
				self._reading = time.time()
			return r != []

	def writing (self):
		while True:
			if self._writing and time.time() > self._writing + self.read_timeout:
				self.close()
				self.logger.wire("%15s peer is too slow" % self.peer)
				raise TooSlowError('Waited for data on a socket for more than %d second(s)' % self.read_timeout)

			try:
				_,w,_ = select.select([],[self.io,],[],0)
			except select.error,e:
				if e.args[0] not in error.block:
					self.close()
					self.logger.wire("%15s errno %s on socket" % (self.peer,errno.errorcode[e.args[0]]))
					raise NetworkError('errno %s on socket' % errno.errorcode[e.args[0]])
				return False

			if w:
				self._writing = time.time()
			return w != []

	def _reader (self,number):
		if not self.io:
			self.close()
			raise NotConnected('Trying to read on a close TCP conncetion')
		if number == 0:
			yield ''
			return
		while not self.reading():
			yield ''
		try:
			read = ''
			while number:
				if self._reading is None:
					self._reading = time.time()
				elif time.time() > self._reading + self.read_timeout:
					self.close()
					self.logger.wire("%15s peer is too slow" % self.peer)
					raise TooSlowError('Waited for data on a socket for more than %d second(s)' % self.read_timeout)
				read = self.io.recv(number)
				number -= len(read)
				if not read:
					self.close()
					self.logger.wire("%15s lost TCP session with peer" % self.peer)
					raise LostConnection('Lost the TCP connection')
				yield read
			self.logger.wire(LazyFormat("Peer %15s RECEIVED " % self.peer,od,read))
			self._reading = None
		except socket.timeout,e:
			self.close()
			self.logger.wire("%15s peer is too slow" % self.peer)
			raise TooSlowError('Timeout while reading data from the network: %s ' % strerrno(e))
		except socket.error,e:
			self.close()
			self.logger.wire("%15s undefined error on socket" % self.peer)
			if e.args[0] == errno.EPIPE:
				raise LostConnection('Lost the TCP connection')
			raise NetworkError('Problem while reading data from the network: %s ' % strerrno(e))

	def writer (self,data):
		if not self.io:
			# XXX: FIXME: Make sure it does not hold the cleanup during the closing of the peering session
			yield True
			return
		if not self.writing():
			yield False
			return
		try:
			self.logger.wire(LazyFormat("Peer %15s SENDING " % self.peer,od,data))
			# we can not use sendall as in case of network buffer filling
			# it does raise and does not let you know how much was sent
			sent = 0
			length = len(data)
			while sent < length:
				if self._writing is None:
					self._writing = time.time()
				elif time.time() > self._writing + self.read_timeout:
					self.close()
					self.logger.wire("%15s peer is too slow" % self.peer)
					raise TooSlowError('Waited for data on a socket for more than %d second(s)' % self.read_timeout)
				try:
					nb = self.io.send(data[sent:])
					if not nb:
						self.close()
						self.logger.wire("%15s lost TCP connection with peer" % self.peer)
						raise LostConnection('lost the TCP connection')
					sent += nb
					yield False
				except socket.error,e:
					if e.args[0] not in error.block:
						self.logger.wire("%15s problem sending message, errno %s" % (self.peer,str(e.args[0])))
						raise NetworkError('Problem while reading data from the network: %s ' % strerrno(e))
					if sent == 0:
						self.logger.wire("%15s problem sending message, errno %s, will retry later" % (self.peer,errno.errorcode[e.args[0]]))
						yield False
					else:
						self.logger.wire("%15s blocking io problem mid-way sending through a message, trying to complete" % self.peer)
						yield False
			self._writing = None
			yield True
			return
		except socket.error, e:
			# Must never happen as we are performing a select before the write
			#failure = getattr(e,'errno',None)
			#if failure in error.block:
			#	return False
			self.close()
			self.logger.wire("%15s %s" % (self.peer,trace()))
			if e.errno == errno.EPIPE:
				# The TCP connection is gone.
				raise NetworkError('Broken TCP connection')
			else:
				raise NetworkError('Problem while writing data to the network: %s' % strerrno(e))

	def reader (self):
		header = ''
		for part in self._reader(Message.HEADER_LEN):
			header += part
			if len(header) != Message.HEADER_LEN:
				yield 0,0,'',''

		if not header.startswith(Message.MARKER):
			raise ValueError('1 1 The packet received does not contain a BGP marker')

		msg = ord(header[18])
		length = unpack('!H',header[16:18])[0]

		if length < Message.HEADER_LEN or length > Message.MAX_LEN:
			raise ValueError('1 2 %s has an invalid message length of %d' %(Message().name(msg),length))

		validator = Message.Length.get(msg,lambda _ : _ >= 19)
		if not validator(length):
			# MUST send the faulty msg_length back
			raise ValueError('1 2 %s has an invalid message length of %d' %(Message().name(msg),msg_length))

		body = ''
		number = length - Message.HEADER_LEN
		for part in self._reader(number):
			body += part
			if len(body) != number:
				yield 0,0,'',''

		yield length,msg,header,body