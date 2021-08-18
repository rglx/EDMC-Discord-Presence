# EDMC plugin for reporting a rich presence on discord which will include some basic location information and 

plugin_name = "DiscordPresence"
plugin_version = "4.0.0-beta1"
plugin_author = "garud, rglx"
plugin_license = "Apache, 2.0"

# removed config interfacing stuff. if you installed the plugin and then disabled it.... well, there's no reason for that

import functools, l10n # localization tools
import logging # edmc hooks into this and makes logging super easy
import time # getting current timestamp for certain things
import threading # for discord SDK wrapper
from os.path import dirname, join # used in locating our plugin directory and portably getting it for use with the SDK
import tkinter as tk # base tkinter stuff
from config import appname # just need the appname for the logger
from py_discord_sdk import discordsdk as dsdk # discord client SDK wrapper (this is the big one)

# setup EDMC logger
logger = logging.getLogger(f'{appname}.{plugin_name}')

# set up translation system (HELP WANTED PLEASE SEE THE L10n FOLDER)
_ = functools.partial(l10n.Translations.translate, context=__file__)


class DiscordPresence:

	def __init__(self):

		self.reportedBody = None
		self.reportedActivity = None
		self.reportedIsOdyssey = None
		self.reportedLandingPad = None

		# retrieve from an application you create at https://discord.com/developers/applications
		self.discordApplicationId = 386149818227097610

		# overwritten on plugin UI initialization
		self.pluginLabel = None
		self.pluginLabelRight = None

		self.plugin_dir = None # overwritten on plugin start, points to EDMC's plugins folder
		self.plugin_path = None # points to our specific plugin's folder

		# massive pile of threads and interfaces that i really don't understand very well (magic, obviously)
		self.activity_manager = None # handles discord sdk communication
		self.activity = {} # contents of activity information
		self.call_back_thread = None # handles responses from discord SDK
		self.discord_thread = None # handles starting and in part, management of the SDK
		self.discordSdkInterface = None # the actual discord SDK interface itself

		self.currentPresenceState = _("Plugin initializing...")
		self.currentPresenceDetails = _("{plugin_name} v{plugin_version}, by {plugin_author}").format(plugin_name=plugin_name,plugin_version=plugin_version,plugin_author=plugin_author)
		self.currentPresenceTimestamp = int(time.time())

		logger.info("instantiated an instance of "+plugin_name+"'s classcode")

	# plugin initialization
	def plugin_start3(self, plugin_dir):
		# create the thread that'll hold our discord API instance and send it off
		self.plugin_dir = plugin_dir
		self.discord_thread = threading.Thread(target=self.check_run, args=(self.plugin_dir,))
		self.discord_thread.setDaemon(True)
		self.discord_thread.start()
		return plugin_name

	# plugin shutdown
	def plugin_stop(self):
		self.pluginLabelRight["text"] = "Shutting down Discord API..."
		self.activity_manager.clear_activity(self.callback)
		self.call_back_thread = None

	# main window additions
	def plugin_app(self, parent):
		self.pluginLabel = tk.Label(parent, text="Discord:")
		self.pluginLabelRight = tk.Label(parent, text="starting plugin, v"+ plugin_version, anchor=tk.W)
		return self.pluginLabel, self.pluginLabelRight

	# incoming journal entry from the game. fields like 'station' sometimes are not filled
	def journal_entry(self, cmdr, is_beta, system, station, entry, state):
		# copy our old states to compare against if they're changed instead of changing them and messing with the API if they're unchanged
		newPresenceState = self.currentPresenceState
		newPresenceDetails = self.currentPresenceDetails

		# retrieve our global information

		# get our current station on the off chance it's already set
		# this might get a little strange if we're approaching a Horizons station or a dockable Odyssey station and EDMC picks up on it
		if station is not None:
			self.reportedBody = station

		# i suspect "loadgame" event will return "Odyssey": True even in horizons so we're not going to use it here.
		if entry["event"] == "Fileheader":
			self.reportedIsOdyssey = False
			# but we should still account for the fact that Horizons straight-up won't have an "Odyssey" entry field
			if "Odyssey" in entry.keys():
				self.reportedIsOdyssey == boolean(entry["Odyssey"])
			logger.info("Game is Odyssey? "+self.reportedIsOdyssey)

		if entry["event"] == "Location":
			if entry["Docked"]:
				self.reportedBody = entry["StationName"]
			else:
				self.reportedBody = entry["Body"]
			# if EDMC has a "system" known already this is overwritten below, and by design a 'Location' journal entry assures this.
			# left just in case something goes wrong with EDMC or the game's journal events
			newPresenceDetails = _("Unknown System")


		# instance hopping stuff
		elif entry["event"] == "StartJump":
			self.reportedActivity = None
			self.reportedBody = None
			# starting a new jump (to SC or to witchspace)
			if entry["JumpType"] == "Supercruise":
				# entering supercruise
				newPresenceDetails = _("Entering supercruise")
			elif entry["JumpType"] == "Hyperspace":
				# entering hyperspace
				newPresenceDetails = _("Entering witchspace")
			else:
				# ... something else? dunno.
				newPresenceDetails = _("Jumping ... somewhere?")


		elif entry["event"] == "FSDJump":
			# exiting witchspace into a new system
			newPresenceDetails = _("In supercruise")
			self.reportedActivity = None
			self.reportedBody = None

		elif entry["event"] == "SupercruiseExit":
			# exiting supercruise somewhere
			self.reportedBody = entry["Body"]
			if self.reportedActivity == "OrbitalCruise":
				newPresenceDetails = _("Flying around the surface")
			else:
				newPresenceDetails = _("Flying in deep space")

		elif entry["event"] == "DockingGranted":
			# cmdr requested docking & station authorized it
			self.reportedLandingPad = str(entry["LandingPad"])
			newPresenceDetails = _("Docking to {stationName}").format(stationName=entry["StationName"])
			if self.reportedLandingPad != None:
				newPresenceDetails += _(" (pad #{landingPadNumber})").format(landingPadNumber=self.reportedLandingPad) # PLEASE MAKE SURE, IN THE TRANSLATIONS, THAT THE LEADING SPACE IS STILL THERE.

		elif entry["event"] == "DockingCancelled" or entry["event"] == "DockingDenied" or entry["event"] == "DockingRequested":
			# cmdr cancelled docking authorization
			#   or station refused/revoked docking request (due to distance or shooting people or whatever)
			#   or docking requested by cmdr
			#   (these events all mean the same thing)
			newPresenceDetails = _("Flying near {stationName}").format(stationName=entry["StationName"])
			self.reportedLandingPad = None

		elif entry["event"] == "Docked":
			# cmdr has either logged in docked or just docked after flying to the station
			# (or rebought and is now docked)
			newPresenceDetails = _("Docked at {stationName}").format(stationName=entry["StationName"])
			if self.reportedLandingPad != None:
				newPresenceDetails += _(" (pad #{landingPadNumber})").format(landingPadNumber=self.reportedLandingPad) # PLEASE MAKE SURE, IN THE TRANSLATIONS, THAT THE LEADING SPACE IS STILL THERE.

		elif entry["event"] == "Undocked":
			# cmdr launching from a landing pad
			newPresenceDetails = _("Launching from {stationName}").format(stationName=entry["StationName"])
			if self.reportedLandingPad != None:
				newPresenceDetails += _(" (pad #{landingPadNumber})").format(landingPadNumber=self.reportedLandingPad) # PLEASE MAKE SURE, IN THE TRANSLATIONS, THAT THE LEADING SPACE IS STILL THERE.
			self.reportedLandingPad = None


		elif entry["event"] == "SupercruiseEntry":
			# entering supercruise
			newPresenceDetails = _("In supercruise")
			self.reportedActivity = None
			self.reportedBody = None

		elif entry["event"] == "ApproachBody":
			# entering orbital cruise of a planet
			self.reportedBody = entry["Body"]
			newPresenceDetails = _("In orbital cruise")
			self.reportedActivity = "OrbitalCruise"

		elif entry["event"] == "ApproachSettlement":
			# entering vicinity of an odyssey settlement
			self.reportedBody = entry["Name"] # don't include planet body name, just station


		elif entry["event"] == "Touchdown" or entry["event"] == "SRVDestroyed":
			# GOOOOOOOOOOOOOOOOOOOOOOOOOOOOOAAAAAAAAAALLLLLLLLLLLLLLLLL
			# landing on the surface of a planet
			newPresenceDetails = _("Landed on the surface")

		elif entry["event"] == "Liftoff":
			# flying up into the sun like a piece of garbage
			newPresenceDetails = _("Flying above the surface")

		elif entry["event"] == "LaunchSRV":
			newPresenceDetails = _("Driving on the surface")

		# todo: find srv retrieval event name

		elif entry["event"] == "Disembark":
			# leaving your ship/srv/taxi on foot
			newPresenceDetails = _("Walking around")

		elif entry["event"] == "Embark": 
			# embarking into a ship SHOULD produce a location event or similar...not sure about dropships/taxis, that may need investigating
			if entry["SRV"]:
				newPresenceDetails = _("Driving on the surface")

		# todo: find dropship-related and taxi-related events and account for them here.

		# todo: wait for frontier to implement a journal event that is sent for when a fleet carrier we're docked to starts jumping somewhere else
		# coding in something for the "carrierjump" event makes sense and all, but the 'system' above is updated when we get there by other events,
		# but our body (and our docked state) remains the same (but not our landing pad. that is almost always different)
		elif entry["event"] == "CarrierJump":
			newPresenceDetails = _("Docked at {stationName}").format(stationName=entry["StationName"])
			self.reportedLandingPad = None # so unset it because ships shuffle around landing pads when a carrier jumps



		elif entry["event"] == "Died":
			self.reportedBody = None
			self.reportedLandingPad = None
			self.reportedActivity = None
			newPresenceDetails = _("Dead!") # :(

		if system != None: # only report our system if we have it to begin with. otherwise just ignore it.
			if self.reportedBody == None:
				newPresenceState = _("In {system}").format(system=system)
			else:
				# because saying "In Parrot's Head Sector blah blah, near Parrot's Head Sector blah blah" gets really unnecessary
				# might get weird if you have stations or bodies that start with the system's name that shouldn't have it removed.
				if self.reportedBody.startswith(system):
					self.reportedBody = self.reportedBody.replace(system,_("body"),1) # only do this once
				newPresenceState = _("In {system}, near {nearby}").format(system=system,nearby=self.reportedBody)


		if newPresenceState != self.currentPresenceState or newPresenceDetails != self.currentPresenceDetails:
			self.currentPresenceState = newPresenceState
			self.currentPresenceDetails = newPresenceDetails
			self.currentPresenceTimestamp = int(time.time()) # update the time as well

			# update our plugin UI as well
			self.pluginLabelRight["text"] = newPresenceDetails + "\n" + newPresenceState
			self.update_presence()

	# update our presence in Discord itself via the SDK wrapper
	def update_presence(self):
		self.activity.state = self.currentPresenceDetails
		self.activity.details = self.currentPresenceState
		self.activity.timestamps.start = int(self.currentPresenceTimestamp)
		self.activity_manager.update_activity(self.activity, self.callback)


	# handles returned errors/OK-statuses from the SDK
	def callback(self, result):
		#logger.info(f'Callback: {result}')
		if result == dsdk.Result.ok:
			logger.info(f'Successfully set the activity! Code: {result}')
		elif result == dsdk.Result.transaction_aborted:
			logger.warning(f'Transaction aborted due to SDK shutting down: {result}')
		else:
			logger.error(f'Error in callback: {result}')
			raise Exception(result)

	# initial setup and startup
	def check_run(self, plugin_dir):
		# get our current directory so the SDK wrapper knows where to find the compiled libraries
		self.plugin_path = join(dirname(plugin_dir), plugin_name)

		# set up our SDK's instance so we can fuck around with it
		retry = True
		while retry:
			time.sleep(1 / 10)
			try:
				self.discordSdkInterface = dsdk.Discord(self.discordApplicationId, dsdk.CreateFlags.no_require_discord, self.plugin_path)
				retry = False
			except Exception:
				pass

		# make it do the thing
		self.activity_manager = self.discordSdkInterface.get_activity_manager()
		self.activity = dsdk.Activity()

		self.call_back_thread = threading.Thread(target=self.run_callbacks)
		self.call_back_thread.setDaemon(True)
		self.call_back_thread.start()

		# update discord-visible fields with their initial values
		self.currentPresenceState = _("Connecting to game...")
		self.currentPresenceDetails = _("{plugin_name} v{plugin_version}, by {plugin_author}").format(plugin_name=plugin_name,plugin_version=plugin_version,plugin_author=plugin_author)
		self.currentPresenceTimestamp = time.time()

		self.update_presence()

	# keeps the SDK API alive, and if it's dead, restarts it
	def run_callbacks(self):
		try:
			while True:
				time.sleep(1 / 10)
				self.discordSdkInterface.run_callbacks()
		except Exception:
			self.check_run(self.plugin_dir)

plugin = DiscordPresence()

def plugin_start3(plugin_dir):
	return plugin.plugin_start3(plugin_dir)

def plugin_stop():
	return plugin.plugin_stop()

def plugin_app(parent):
	return plugin.plugin_app(parent)

def journal_entry(cmdr, is_beta, system, station, entry, state):
	return plugin.journal_entry( cmdr, is_beta, system, station, entry, state )
