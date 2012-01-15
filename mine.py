#!/usr/bin/python
# Copyright (C) 2011 by fpgaminer <fpgaminer@bitcoin-mining.com>
#                       fizzisist <fizzisist@fpgamining.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from ft232r import FT232R, FT232R_PortList
from jtag import JTAG
from ConsoleLogger import ConsoleLogger
from rpcClient import RPCClient
import time
from optparse import OptionParser
import traceback
from threading import Thread, Lock
from Queue import Queue, Empty, Full
from struct import pack, unpack
from hashlib import sha256

NUM_RETRIES = 5
USER_INSTRUCTION = 0b000010

# Option parsing:
parser = OptionParser(usage="%prog [-d <devicenum>] [-c <chain>] -u <pool-url> -w <user:pass>")
parser.add_option("-d", "--devicenum", type="int", dest="devicenum", default=None,
                  help="Device number, optional. Opens the first available device by default")
parser.add_option("-c", "--chain", type="int", dest="chain", default=2,
                  help="JTAG chain number, can be 0, 1, or 2 for both FPGAs on the board (default 2)")
parser.add_option("-i", "--interval", type="int", dest="getwork_interval", default=20,
                  help="Getwork interval in seconds (default 20)")
parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                  help="Verbose logging")
parser.add_option("-u", "--url", type="str", dest="url",
                  help="URL for the pool or bitcoind server, e.g. pool.com:8337")
parser.add_option("-w", "--worker", type="str", dest="worker",
                  help="Worker username and password for the pool, e.g. user:pass")
settings, args = parser.parse_args()

class Object(object):
	pass

# Convert a hex string into an array of bytes
def hexstr2array(hexstr):
	arr = []

	for i in range(len(hexstr)/2):
		arr.append((int(hexstr[i*2], 16) << 4) | int(hexstr[i*2+1], 16))
	
	return arr

# Convert an integer to an array of bits.
# LSB first.
def int2bits(i, bits):
	result = []

	for n in range(bits):
		result.append(i & 1)
		i = i >> 1
	
	return result

# LSB first.
def bits2int(bits):
	x = 0

	for i in range(len(bits)):
		x |= bits[i] << i
	
	return x
	
def checkTarget(target, hashOutput):
	for t,h in zip(target[::-1], hashOutput[::-1]):
		if ord(t) > ord(h):
			return True
		elif ord(t) < ord(h):
			return False
	return True

def checkNonce(gold):
	staticDataUnpacked = unpack('<' + 'I'*19, gold.job.data.decode('hex')[:76])
	staticData = pack('>' + 'I'*19, *staticDataUnpacked)
	hashInput = pack('>76sI', staticData, gold.nonce)
	hashOutput = sha256(sha256(hashInput).digest()).digest()
	
	base_target = 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffff00000000'
	
	if checkTarget(base_target.decode('hex'), hashOutput):
		logger.reportValid(gold.chain)
	else:
		logger.reportError(hex(gold.nonce)[2:], gold.chain)
		return False
	
	if checkTarget(gold.job.target.decode('hex'), hashOutput):
		return True
	
	return False
	
def fpgaReadByte(jtag):
	bits = int2bits(0, 13)
	byte = bits2int(jtag.read_dr(bits))
	return byte

def fpgaReadNonce(jtag):
	jtag.tap.reset()
	jtag.instruction(USER_INSTRUCTION)
	jtag.shift_ir()

	# Sync to the beginning of a nonce.
	# The MSB is a VALID flag. If 0, data is invalid (queue empty).
	# The next 4-bits indicate which byte of the nonce we got.
	# 1111 is LSB, and then 0111, 0011, 0001.
	byte = None
	while True:
		byte = fpgaReadByte(jtag)

		# check data valid bit:
		if byte < 0x1000:
			jtag.tap.reset()
			return None
		
		#logger.reportDebug("(FPGA%d) Read: %04x" % (jtag.chain, byte))
		
		# check byte counter:
		if (byte & 0xF00) == 0xF00:
			break
	
	# We now have the first byte
	nonce = byte & 0xFF
	count = 1
	#logger.reportDebug("(FPGA%d) Potential nonce, reading the rest..." % jtag.chain)
	while True:
		byte = fpgaReadByte(jtag)
		
		#logger.reportDebug("(FPGA%d) Read: %04x" % (jtag.chain, byte))
		
		# check data valid bit:
		if byte < 0x1000:
			jtag.tap.reset()
			return None
		
		# check byte counter:
		if (byte & 0xF00) >> 8 != (0xF >> count):
			jtag.tap.reset()
			return None
		
		nonce |= (byte & 0xFF) << (count * 8)
		count += 1
		
		if (byte & 0xF00) == 0x100:
			break

	jtag.tap.reset()

	#logger.reportDebug("Nonce completely read: %08x" % nonce)

	return nonce

# TODO: This may not actually clear the queue, but should be correct most of the time.
def fpgaClearQueue(jtag):
	logger.reportDebug("(FPGA%d) Clearing queue..." % jtag.chain)

	jtag.tap.reset()
	jtag.instruction(USER_INSTRUCTION)
	jtag.shift_ir()
	
	while True:
		if fpgaReadByte(jtag) < 0x1000:
			break
	jtag.tap.reset()
	
	logger.reportDebug("(FPGA%d) Queue cleared" % jtag.chain)

def fpgaWriteJob(jtag, job):
	# We need the 256-bit midstate, and 12 bytes from data.
	# The first 64 bytes of data are already hashed (hence midstate),
	# so we skip that. Of the last 64 bytes, 52 bytes are constant and
	# not needed by the FPGA.
	
	#start_time = time.time()
	
	midstate = hexstr2array(job.midstate)
	data = hexstr2array(job.data)[64:64+12]

	# Job's hex strings are LSB first, and the FPGA wants them MSB first.
	midstate.reverse()
	data.reverse()

	#logger.reportDebug("(FPGA%d) Loading job data..." % jtag.chain)

	#jtag._setAsyncMode()
	
	jtag.instruction(USER_INSTRUCTION)
	jtag.shift_ir()

	data = midstate + data + [0]

	for i in range(len(data)):
		x = data[i]

		if i != 0:
			x = 0x100 | x
			
		jtag.shift_dr(int2bits(x, 13))
	
	jtag.tap.reset()

	ft232r.flush()
	
	#logger.reportDebug("(FPGA%d) Job data loaded in %.3f seconds" % (jtag.chain, (time.time() - start_time)))
	logger.reportDebug("(FPGA%d) Job data loaded" % jtag.chain)
	
def handleNonce(job, nonce, chain):
	logger.reportNonce(chain)
	gold = Object()
	gold.chain = chain
	gold.job = job
	gold.nonce = nonce & 0xFFFFFFFF
	try:
		noncequeue.put(gold, block=True, timeout=10)
	except Full:
		logger.log("(FPGA%d) Queue full! Lost a nonce!" % chain)
		
def nonceLoop():
	while True:
		gold = noncequeue.get(block=True)
			
		if checkNonce(gold):
			try:
				goldqueue.put(gold, block=True, timeout=10)
				#logger.reportDebug("(FPGA%d) goldqueue loaded (%d)" % (chain, goldqueue[chain].qsize()))
			except Full:
				logger.log("(FPGA%d) Queue full! Lost a valid nonce!" % gold.chain)


if settings.url is None:
	print "ERROR: URL not specified!"
	parser.print_usage()
	exit()
if settings.worker is None:
	print "ERROR: Worker not specified!"
	parser.print_usage()
	exit()

jobqueue = [None]*2
goldqueue = Queue()

logger = ConsoleLogger(settings.chain, settings.verbose)
rpcclient = RPCClient(settings, logger, jobqueue, goldqueue)

try:
	with FT232R() as ft232r:
		portlist = FT232R_PortList(7, 6, 5, 4, 3, 2, 1, 0)
		ft232r.open(settings.devicenum, portlist)
		
		logger.reportOpened(ft232r.devicenum, ft232r.serial)
		
		if settings.chain == 0 or settings.chain == 1:
			chain_list = [settings.chain]
		elif settings.chain == 2:
			chain_list = [0, 1]
		else:
			logger.log("ERROR: Invalid chain option!", False)
			parser.print_usage()
			exit()
			
		rpcclient.set_chain_list(chain_list)
		
		jtag = [None, None]
		fpga_num = 0
		for chain in chain_list:
			jtag[chain] = JTAG(ft232r, portlist.chain_portlist(chain), chain)
			
			logger.reportDebug("Discovering JTAG chain %d ..." % chain, False)
			jtag[chain].detect()
			
			logger.reportDebug("Found %i device%s ..." % (jtag[chain].deviceCount,
				's' if jtag[chain].deviceCount != 1 else ''), False)

			for idcode in jtag[chain].idcodes:
				msg = " FPGA" + str(chain) + ": "
				msg += JTAG.decodeIdcode(idcode)
				logger.reportDebug(msg, False)
				fpga_num += 1
		
		logger.log("Connected to %d FPGAs" % fpga_num, False)
		
		for chain in chain_list:
			fpgaClearQueue(jtag[chain])
		
		logger.start()
		
		rpcclient.start()
		
		for chain in chain_list:
			jobqueue[chain] = Queue(maxsize=1) # store at most one job
		
		noncequeue = Queue()
		nonceThread = Thread(target=nonceLoop)
		nonceThread.daemon = True
		nonceThread.start()

		current_job = [None]*2
		job = None
		while True:
			time.sleep(0.2)
			logger.updateStatus()
			
			for chain in chain_list:
				nonce = None
				try:
					#logger.reportDebug("(FPGA%d) Checking for new job..." % chain)
					job = jobqueue[chain].get(False)
				except Empty:
					job = None
				
				if job is not None:
					#logger.reportDebug("(FPGA%d) Loading new job..." % chain)
					if current_job[chain] is not None:
						#logger.reportDebug("(FPGA%d) Checking for nonce*..." % chain)
						nonce = fpgaReadNonce(jtag[chain])
					#logger.reportDebug("(FPGA%d) Writing job..." % chain)
					fpgaWriteJob(jtag[chain], job)
					#fpgaClearQueue(jtag[chain])
					if nonce is not None:
						handleNonce(current_job[chain], nonce, chain)
					current_job[chain] = job
				
				if current_job[chain] is not None:
					#logger.reportDebug("(FPGA%d) Checking for nonce..." % chain)
					nonce = fpgaReadNonce(jtag[chain])
					if nonce is not None:
						handleNonce(current_job[chain], nonce, chain)

except KeyboardInterrupt:
	logger.log("Exiting...")
	logger.printSummary(settings)
