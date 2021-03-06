import logging
import subprocess
import errno
import os
import sys
import signal
import atexit

import objc
import Foundation
import AppKit

# Timer interval in seconds.
_TIMER_INTERVAL = 0.010

# Timer interval in milliseconds.
_TIMER_INTERVAL_IN_MS = int( _TIMER_INTERVAL * 1000 )

KEYCODE_CAPITAL = -1
KEYCODE_SPACE = 49
KEYCODE_LSHIFT = -1
KEYCODE_RSHIFT = -1
KEYCODE_LCONTROL = -1
KEYCODE_RCONTROL = -1
KEYCODE_LWIN = -1
KEYCODE_RWIN = -1
KEYCODE_RETURN = 36
KEYCODE_ESCAPE = 53
KEYCODE_TAB = 48
KEYCODE_BACK = 51
KEYCODE_DOWN = 125
KEYCODE_UP = 126

EVENT_KEY_UP = 0
EVENT_KEY_DOWN = 1
EVENT_KEY_QUASIMODE = 2

KEYCODE_QUASIMODE_START = 0
KEYCODE_QUASIMODE_END = 1
KEYCODE_QUASIMODE_CANCEL = 2

CASE_INSENSITIVE_KEYCODE_MAP = {
    29: "0",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    23: "5",
    22: "6",
    26: "7",
    28: "8",
    25: "9",
    KEYCODE_SPACE: " ",
    0: "a",
    11: "b",
    8: "c",
    2: "d",
    14: "e",
    3: "f",
    5: "g",
    4: "h",
    34: "i",
    38: "j",
    40: "k",
    37: "l",
    46: "m",
    45: "n",
    31: "o",
    35: "p",
    12: "q",
    15: "r",
    1: "s",
    17: "t",
    32: "u",
    9: "v",
    13: "w",
    7: "x",
    16: "y",
    6: "z",
    44: "?",
    42: "\\",
    47: ".",
    41: ":",
    24: "+",
    27: "-",
    }

def _getRunningProcessInfo():
    popen = subprocess.Popen( ["ps", "-ef"], stdout=subprocess.PIPE )
    output, errors = popen.communicate()
    info = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        pid = int( parts[1] )
        cmd = " ".join( parts[7:] )
        info.append( {"pid" : pid, "cmd" : cmd} )
    return info

class _KeyNotifierController( object ):
    def __init__( self ):
        pass

    def __killExistingKeyNotifiers( self ):
        # TODO: This may get processes that aren't the key notifier,
        # e.g. 'nano EnsoKeyNotifier.m'.
        infos = [ info for info in _getRunningProcessInfo()
                  if "EnsoKeyNotifier" in info["cmd"] ]
        for info in infos:
            logging.info( "Killing existing key notifier %d." % info["pid"] )
            os.kill( info["pid"], signal.SIGKILL )

    def __tryToStartKeyNotifier( self, path="" ):
        fullPath = os.path.join( path, "EnsoKeyNotifier" )
        logging.info( "Trying to launch '%s'." % fullPath )
        popen = subprocess.Popen( [fullPath] )
        return popen

    def start( self ):
        self.__killExistingKeyNotifiers()
        try:
            # First see if the key notifier is on our path...
            popen = self.__tryToStartKeyNotifier()
        except OSError, e:
            if e.errno == errno.ENOENT:
                logging.info( "Couldn't find key notifier on path." )
                # Maybe we're running from a repository checkout...
                import enso
                path = os.path.normpath( enso.__path__[0] + "/../bin" )
                popen = self.__tryToStartKeyNotifier( path )
            else:
                raise

        self._pid = popen.pid

    def stop( self ):
        logging.info( "Stopping key notifier." )
        try:
            os.kill( self._pid, signal.SIGINT )
        except OSError, e:
            if e.errno == errno.ESRCH:
                logging.warn( "Key notifier process no longer exists." )
            else:
                raise

class _Timer( Foundation.NSObject ):
    def initWithCallback_( self, callback ):
        self = super( _Timer, self ).init()
        if self == None:
            return None
        self.__callback = callback
        return self

    def onTimer( self ):
        self.__callback()

class _AppDelegate( Foundation.NSObject ):

    def applicationShouldTerminate_( self, sender ):
        logging.info( "applicationShouldTerminate() called." )
        return AppKit.NSTerminateNow

    def applicationWillTerminate_( self, notification ):
        # The sys.exitfunc() won't get called unless we explicitly
        # call it here, because OS X is going to terminate our app,
        # not Python.
        sys.exitfunc()

class _KeyListener(Foundation.NSObject):

    def initWithInputManager_( self, inputManager ):
        self = super( _KeyListener, self ).init()
        if self == None:
            return None
        self.__inputManager = inputManager
        return self

    @objc.typedSelector('Vv8@0:4')
    def quasimodeStart(self):
        self.__inputManager.onKeypress( EVENT_KEY_QUASIMODE,
                                        KEYCODE_QUASIMODE_START )

    @objc.typedSelector('Vv8@0:4')
    def quasimodeEnd(self):
        self.__inputManager.onKeypress( EVENT_KEY_QUASIMODE,
                                        KEYCODE_QUASIMODE_END )

    @objc.typedSelector('Vv8@0:4')
    def someKey(self):
        self.__inputManager.onSomeKey()

    @objc.typedSelector('Vv16@0:4@8@12')
    def keyDownChars_keycode_(self, chars, keycode):
        self.__inputManager.onKeypress(EVENT_KEY_DOWN, keycode)

    @objc.typedSelector('Vv16@0:4@8@12')
    def keyUpChars_keycode_(self, chars, keycode):
        self.__inputManager.onKeypress(EVENT_KEY_UP, keycode)

    def register( self ):
        connection = Foundation.NSConnection.defaultConnection()
        connection.setRootObject_(self)
        if connection.registerName_("ensoKeyListener")==objc.NO:
            logging.warn("Couldn't start enso nsconnection")

def nestedAutoreleasePooled(func):
    """
    Decorator that executes the wrapped function in a nested
    autorelease pool, so that any Objective-C memory that is unneeded
    is released when the function returns, rather than at some unknown
    later time.
    """

    def wrappedFunc(*args, **kwargs):
        pool = AppKit.NSAutoreleasePool.alloc().init()
        retval = func(*args, **kwargs)
        del pool
        return retval
    return wrappedFunc

class InputManager( object ):
    def __init__( self ):
        self.__mouseEventsEnabled = False
        self.__qmKeycodes = [0, 0, 0]
        self.__isModal = False
        self.__inQuasimode = False

    @nestedAutoreleasePooled
    def __timerCallback( self ):
        self.onTick( _TIMER_INTERVAL_IN_MS )

    def run( self ):
        logging.info( "Entering InputManager.run()" )

        app = AppKit.NSApplication.sharedApplication()

        if not app.delegate():
            logging.info( "Attaching app delegate." )
            delegate = _AppDelegate.alloc().init()
            app.setDelegate_( delegate )
        else:
            logging.info( "An app delegate is already attached; "
                          "skipping installation." )

        timer = _Timer.alloc().initWithCallback_(self.__timerCallback)
        signature = timer.methodSignatureForSelector_(timer.onTimer)
        invocation = Foundation.NSInvocation.invocationWithMethodSignature_(signature)
        invocation.setSelector_(timer.onTimer)
        invocation.setTarget_(timer)
        Foundation.NSTimer.scheduledTimerWithTimeInterval_invocation_repeats_(
                _TIMER_INTERVAL, invocation, objc.YES)

        keyNotifier = _KeyNotifierController()
        keyNotifier.start()
        atexit.register( keyNotifier.stop )

        keyListener = _KeyListener.alloc().initWithInputManager_(self)
        keyListener.register()

        self.onInit()

        if not app.isRunning():
            logging.info( "Calling app.run()." )
            app.run()
        else:
            logging.info( "Application appears to be running already; "
                          "skipping app.run()." )

    def stop( self ):
        app = AppKit.NSApplication.sharedApplication()
        app.terminate_( None )

    def enableMouseEvents( self, isEnabled ):
        # TODO: Implementation needed.
        self.__mouseEventsEnabled = isEnabled

    def onKeypress( self, eventType, vkCode ):
        pass

    def onSomeKey( self ):
        pass

    def onSomeMouseButton( self ):
        pass

    def onExitRequested( self ):
        pass

    def onMouseMove( self, x, y ):
        pass

    def getQuasimodeKeycode( self, quasimodeKeycode ):
        return self.__qmKeycodes[quasimodeKeycode]

    def setQuasimodeKeycode( self, quasimodeKeycode, keycode ):
        # TODO: Implementation needed.
        self.__qmKeycodes[quasimodeKeycode] = keycode

    def setModality( self, isModal ):
        # TODO: Implementation needed.
        self.__isModal = isModal

    def setCapsLockMode( self, isCapsLockEnabled ):
        # TODO: Implementation needed.
        pass

    def onTick( self, msPassed ):
        pass

    def onInit( self ):
        pass
