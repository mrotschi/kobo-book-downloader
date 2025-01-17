from Globals import Globals
from KoboDrmRemover import KoboDrmRemover

import requests

from typing import Dict, Tuple
import base64
import html
import os
import re
import urllib
import uuid

import colorama

# It was not possible to enter the entire captcha response on MacOS.
# Importing readline changes the implementation of input() and solves the issue.
# See https://stackoverflow.com/q/65735885 and https://stackoverflow.com/q/7357007.
import readline

class KoboException( Exception ):
	pass

# The hook's workflow is based on this:
# https://github.com/requests/toolbelt/blob/master/requests_toolbelt/auth/http_proxy_digest.py
def ReauthenticationHook( r, *args, **kwargs ):
	if r.status_code != requests.codes.unauthorized: # 401
		return

	Globals.Logger.debug( "Refreshing expired authentication token" )

	# Consume content and release the original connection to allow our new request to reuse the same one.
	r.content
	r.close()

	prep = r.request.copy()

	# Refresh the authentication token and use it.
	Globals.Kobo.RefreshAuthentication()
	headers = Kobo.GetHeaderWithAccessToken()
	prep.headers[ "Authorization" ] = headers[ "Authorization" ]

	# Don't retry to reauthenticate this request again.
	prep.deregister_hook( "response", ReauthenticationHook )

	# Resend the failed request.
	_r = r.connection.send( prep, **kwargs )
	_r.history.append( r )
	_r.request = prep

	return _r

class SessionWithTimeOut( requests.Session ):
	def request( self, method, url, **kwargs ):
		if "timeout" not in kwargs:
			kwargs[ "timeout" ] = 30 # 30 seconds
		return super().request( method, url, **kwargs )

class Kobo:
	ApplicationVersion = "8.11.24971"
	DefaultPlatformId = "00000000-0000-0000-0000-000000004000"
	DisplayProfile = "Android"

	def __init__( self ):
		# Use the user agent of the Kobo Android app, otherwise the login request hangs forever.
		headers = {
			"User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Google Nexus 7 2013 Build/MRA58K; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/74.0.3729.186 Safari/537.36 KoboApp/8.40.2.29861 KoboPlatform Id/00000000-0000-0000-0000-000000004000 KoboAffiliate/Kobo KoboBuildFlavor/global",
		}

		self.InitializationSettings = {}
		self.Session = SessionWithTimeOut()
		self.Session.headers.update( headers )

	# This could be added to the session but then we would need to add { "Authorization": None } headers to all other
	# functions that doesn't need authorization.
	@staticmethod
	def GetHeaderWithAccessToken() -> dict:
		authorization = "Bearer " + Globals.Settings.AccessToken
		headers = { "Authorization": authorization }
		return headers

	# This could be added to the session too. See the comment at GetHeaderWithAccessToken.
	@staticmethod
	def __GetReauthenticationHook() -> dict:
		return { "response": ReauthenticationHook }

	# The initial device authentication request for a non-logged in user doesn't require a user key, and the returned
	# user key can't be used for anything.
	def AuthenticateDevice( self, userKey: str = "", affiliate: str="Kobo" ) -> None:
		Globals.Logger.debug( "Kobo.AuthenticateDevice" )

		if len( Globals.Settings.DeviceId ) == 0:
			Globals.Settings.DeviceId = str( uuid.uuid4() )
			Globals.Settings.AccessToken = ""
			Globals.Settings.RefreshToken = ""

		postData = {
			"AffiliateName": affiliate,
			"AppVersion": Kobo.ApplicationVersion,
			"ClientKey": base64.b64encode( Kobo.DefaultPlatformId.encode() ).decode(),
			"DeviceId": Globals.Settings.DeviceId,
			"PlatformId": Kobo.DefaultPlatformId
		}

		if len( userKey ) > 0:
			postData[ "UserKey" ] = userKey

		response = self.Session.post( "https://storeapi.kobo.com/v1/auth/device", json = postData )
		response.raise_for_status()
		jsonResponse = response.json()

		if jsonResponse[ "TokenType" ] != "Bearer":
			raise KoboException( "Device authentication returned with an unsupported token type: '%s'" % jsonResponse[ "TokenType" ] )

		Globals.Settings.AccessToken = jsonResponse[ "AccessToken" ]
		Globals.Settings.RefreshToken = jsonResponse[ "RefreshToken" ]
		if not Globals.Settings.AreAuthenticationSettingsSet():
			raise KoboException( "Authentication settings are not set after device authentication." )

		if len( userKey ) > 0:
			Globals.Settings.UserKey = jsonResponse[ "UserKey" ]

		Globals.Settings.Save()

	def RefreshAuthentication( self ) -> None:
		Globals.Logger.debug( "Kobo.RefreshAuthentication" )

		headers = Kobo.GetHeaderWithAccessToken()

		postData = {
			"AppVersion": Kobo.ApplicationVersion,
			"ClientKey": base64.b64encode( Kobo.DefaultPlatformId.encode() ).decode(),
			"PlatformId": Kobo.DefaultPlatformId,
			"RefreshToken": Globals.Settings.RefreshToken
		}

		# The reauthentication hook is intentionally not set.
		response = self.Session.post( "https://storeapi.kobo.com/v1/auth/refresh", json = postData, headers = headers )
		response.raise_for_status()
		jsonResponse = response.json()

		if jsonResponse[ "TokenType" ] != "Bearer":
			raise KoboException( "Authentication refresh returned with an unsupported token type: '%s'" % jsonResponse[ "TokenType" ] )

		Globals.Settings.AccessToken = jsonResponse[ "AccessToken" ]
		Globals.Settings.RefreshToken = jsonResponse[ "RefreshToken" ]
		if not Globals.Settings.AreAuthenticationSettingsSet():
			raise KoboException( "Authentication settings are not set after authentication refresh." )

		Globals.Settings.Save()

	def LoadInitializationSettings( self ) -> None:
		Globals.Logger.debug( "Kobo.LoadInitializationSettings" )

		headers = Kobo.GetHeaderWithAccessToken()
		hooks = Kobo.__GetReauthenticationHook()
		response = self.Session.get( "https://storeapi.kobo.com/v1/initialization", headers = headers, hooks = hooks )
		response.raise_for_status()
		jsonResponse = response.json()
		self.InitializationSettings = jsonResponse[ "Resources" ]

	def __GetExtraLoginParameters( self, affiliate:str ) -> Tuple[ str, str, str ]:
		Globals.Logger.debug( "Kobo.__GetExtraLoginParameters" )

		signInUrl = self.InitializationSettings[ "sign_in_page" ]

		params = {
			"wsa": affiliate,
			"pwsav": Kobo.ApplicationVersion,
			"pwspid": Kobo.DefaultPlatformId,
			"pwsdid": Globals.Settings.DeviceId
		}

		response = self.Session.get( signInUrl, params = params )
		response.raise_for_status()
		htmlResponse = response.text

		# The link can be found in the response ('<a class="kobo-link partner-option kobo"') but the Android app does not use the entire path.
		# (The entire path looks like this: "/ww/en/signin/signin/kobo?workflowId=01234567-0123-0123-0123-0123456789ab".)
		parsed = urllib.parse.urlparse( signInUrl )
		koboSignInUrl = parsed._replace( query = None, path = "/ww/en/signin/signin" ).geturl()

		match = re.search( r"""/signin/.*\?workflowId=([0-9a-f\-]+)""", htmlResponse )
		if match is None:
			raise KoboException( "Can't find the workflow ID. The page format might have changed." )
		workflowId = html.unescape( match.group( 1 ) )

		match = re.search( r"""<input name="__RequestVerificationToken" type="hidden" value="([^"]+)" />""", htmlResponse )
		if match is None:
			raise KoboException( "Can't find the request verification token in the login form. The page format might have changed." )
		requestVerificationToken = html.unescape( match.group( 1 ) )

		return koboSignInUrl, workflowId, requestVerificationToken

	def Login( self, email: str, password: str, captcha: str, affiliate:str ) -> None:
		Globals.Logger.debug( "Kobo.Login" )

		Globals.Settings.Affiliate = affiliate
		signInUrl, workflowId, requestVerificationToken = self.__GetExtraLoginParameters(affiliate)

		postData = {
			"LogInModel.WorkflowId": workflowId,
			"LogInModel.Provider": affiliate,
			"ReturnUrl": "",
			"__RequestVerificationToken": requestVerificationToken,
			"LogInModel.UserName": email,
			"LogInModel.Password": password,
			"g-recaptcha-response": captcha,
			"h-captcha-response": captcha
		}

		response = self.Session.post( signInUrl, data = postData )
		response.raise_for_status()
		htmlResponse = response.text

		match = re.search( r"'(kobo://UserAuthenticated\?[^']+)';", htmlResponse )
		if match is None:
			raise KoboException( "Authenticated user URL can't be found. The page format might have changed." )

		url = match.group( 1 )
		parsed = urllib.parse.urlparse( url )
		parsedQueries = urllib.parse.parse_qs( parsed.query )
		Globals.Settings.UserId = parsedQueries[ "userId" ][ 0 ] # We don't call Settings.Save here, AuthenticateDevice will do that if it succeeds.
		userKey = parsedQueries[ "userKey" ][ 0 ]

		self.AuthenticateDevice( userKey, affiliate )

	def GetBookInfo( self, productId: str ) -> dict:
		Globals.Logger.debug( "Kobo.GetBookInfo" )

		url = self.InitializationSettings[ "book" ].replace( "{ProductId}", productId )
		headers = Kobo.GetHeaderWithAccessToken()
		hooks = Kobo.__GetReauthenticationHook()

		response = self.Session.get( url, headers = headers, hooks = hooks )
		response.raise_for_status()
		jsonResponse = response.json()
		return jsonResponse

	def __GetMyBookListPage( self, syncToken: str ) -> Tuple[ list, str ]:
		Globals.Logger.debug( "Kobo.__GetMyBookListPage" )

		url = self.InitializationSettings[ "library_sync" ]
		headers = Kobo.GetHeaderWithAccessToken()
		hooks = Kobo.__GetReauthenticationHook()

		if len( syncToken ) > 0:
			headers[ "x-kobo-synctoken" ] = syncToken

		response = Globals.Kobo.Session.get( url, headers = headers, hooks = hooks )
		response.raise_for_status()
		bookList = response.json()

		syncToken = ""
		syncResult = response.headers.get( "x-kobo-sync" )
		if syncResult == "continue":
			syncToken = response.headers.get( "x-kobo-synctoken", "" )

		return bookList, syncToken

	def GetMyBookList( self ) -> list:
		# The "library_sync" name and the synchronization tokens make it somewhat suspicious that we should use
		# "library_items" instead to get the My Books list, but "library_items" gives back less info (even with the
		# embed=ProductMetadata query parameter set).

		fullBookList = []
		syncToken = ""
		while True:
			bookList, syncToken = self.__GetMyBookListPage( syncToken )
			fullBookList += bookList
			if len( syncToken ) == 0:
				break

		return fullBookList

	def GetMyWishList( self ) -> list:
		Globals.Logger.debug( "Kobo.GetMyWishList" )

		items = []
		currentPageIndex = 0

		while True:
			url = self.InitializationSettings[ "user_wishlist" ]
			headers = Kobo.GetHeaderWithAccessToken()
			hooks = Kobo.__GetReauthenticationHook()

			params = {
				"PageIndex": currentPageIndex,
				"PageSize": 100, # 100 is the default if PageSize is not specified.
			}

			response = Globals.Kobo.Session.get( url, params = params, headers = headers, hooks = hooks )
			response.raise_for_status()
			wishList = response.json()

			items.extend( wishList[ "Items" ] )

			currentPageIndex += 1
			if currentPageIndex >= wishList[ "TotalPageCount" ]:
				break

		return items

	def __GetContentAccessBook( self, productId: str, displayProfile: str ) -> dict:
		Globals.Logger.debug( "Kobo.__GetContentAccessBook" )

		url = self.InitializationSettings[ "content_access_book" ].replace( "{ProductId}", productId )
		params = { "DisplayProfile": displayProfile }
		headers = Kobo.GetHeaderWithAccessToken()
		hooks = Kobo.__GetReauthenticationHook()

		response = self.Session.get( url, params = params, headers = headers, hooks = hooks )
		response.raise_for_status()
		jsonResponse = response.json()
		return jsonResponse

	@staticmethod
	def __GetContentKeys( contentAccessBookResponse: dict ) -> Dict[ str, str ]:
		jsonContentKeys = contentAccessBookResponse.get( "ContentKeys" )
		if jsonContentKeys is None:
			return {}

		contentKeys = {}
		for contentKey in jsonContentKeys:
			contentKeys[ contentKey[ "Name" ] ] = contentKey[ "Value" ]
		return contentKeys

	@staticmethod
	def __GetDownloadInfo( productId: str, contentAccessBookResponse: dict ) -> Tuple[ str, bool ]:
		jsonContentUrls = contentAccessBookResponse.get( "ContentUrls" )
		if jsonContentUrls is None:
			raise KoboException( "Download URL can't be found for product '%s'." % productId )

		if len( jsonContentUrls ) == 0:
			raise KoboException( "Download URL list is empty for product '%s'. If this is an archived book then it must be unarchived first on the Kobo website (https://www.kobo.com/help/en-US/article/1799/restoring-deleted-books-or-magazines)." % productId )

		for jsonContentUrl in jsonContentUrls:
			if ( jsonContentUrl[ "DRMType" ] == "KDRM" or jsonContentUrl[ "DRMType" ] == "SignedNoDrm" ) and \
				( jsonContentUrl[ "UrlFormat" ] == "EPUB3" or jsonContentUrl[ "UrlFormat" ] == "KEPUB" ):
				hasDrm = jsonContentUrl[ "DRMType" ] == "KDRM"
				return jsonContentUrl[ "DownloadUrl" ], hasDrm

		message = "Download URL for supported formats can't be found for product '%s'.\n" % productId
		message += "Available formats:"
		for jsonContentUrl in jsonContentUrls:
			message += "\nDRMType: '%s', UrlFormat: '%s'" % ( jsonContentUrl[ "DRMType" ], jsonContentUrl[ "UrlFormat" ] )

		raise KoboException( message )

	def __DownloadToFile( self, url, outputPath: str ) -> None:
		Globals.Logger.debug( "Kobo.__DownloadToFile" )

		response = self.Session.get( url, stream = True )
		response.raise_for_status()
		with open( outputPath, "wb" ) as f:
			for chunk in response.iter_content( chunk_size = 1024 * 256 ):
				f.write( chunk )

	# Downloading archived books is not possible, the "content_access_book" API endpoint returns with empty ContentKeys
	# and ContentUrls for them.
	def Download( self, productId: str, displayProfile: str, outputPath: str ) -> None:
		Globals.Logger.debug( "Kobo.Download" )

		jsonResponse = self.__GetContentAccessBook( productId, displayProfile )
		contentKeys = Kobo.__GetContentKeys( jsonResponse )
		try:
			downloadUrl, hasDrm = Kobo.__GetDownloadInfo( productId, jsonResponse )
		except:
			print( colorama.Fore.LIGHTYELLOW_EX + ( "Skipping unkown book type %s." % productId ) + colorama.Fore.RESET )
			return
	
		temporaryOutputPath = outputPath + ".downloading"

		try:
			self.__DownloadToFile( downloadUrl, temporaryOutputPath )

			if hasDrm:
				drmRemover = KoboDrmRemover( Globals.Settings.DeviceId, Globals.Settings.UserId )
				drmRemover.RemoveDrm( temporaryOutputPath, outputPath, contentKeys )
				os.remove( temporaryOutputPath )
			else:
				os.rename( temporaryOutputPath, outputPath )
		except:
			if os.path.isfile( temporaryOutputPath ):
				os.remove( temporaryOutputPath )
			if os.path.isfile( outputPath ):
				os.remove( outputPath )

			raise
