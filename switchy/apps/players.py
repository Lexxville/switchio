# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Common testing call flows
"""
import inspect
from collections import OrderedDict, namedtuple
from ..apps import app
from ..marks import event_callback
from ..utils import get_logger


@app
class TonePlay(object):
    """Play a 'milli-watt' tone on the outbound leg and echo it back
    on the inbound
    """
    @event_callback('CHANNEL_PARK')
    def on_park(self, sess):
        if sess.is_inbound():
            sess.answer()

        # play infinite tones on calling leg
        if sess.is_outbound():
            sess.broadcast('playback::{loops=-1}tone_stream://%(251,0,1004)')

    @event_callback("CHANNEL_ANSWER")
    def on_answer(self, sess):
        # inbound leg simply echos back the tone
        if sess.is_inbound():
            sess.echo()


RecInfo = namedtuple("RecInfo", "host caller callee")


@app
class PlayRec(object):
    '''Play a recording to the callee and record it onto the local file system

    This app can be used in tandem with MOS scoring to verify audio quality.
    The filename provided must exist in the FreeSWITCH sounds directory such
    that ${FS_CONFIG_ROOT}/${sound_prefix}/<category>/<filename> points to a
    valid wave file.
    '''
    def prepost(
        self,
        client,
        filename='ivr-founder_of_freesource.wav',
        category='ivr',
        clip_length=4.25,  # measured empirically for the clip above
        sample_rate=8000,
        iterations=1,  # number of times the speech clip will be played
        callback=None,
        rec_rate=1,  # in calls/recording (i.e. 10 is 1 recording per 10 calls)
        rec_stereo=False,
    ):
        self.filename = filename
        self.category = category
        self.framerate = sample_rate
        self.clip_length = clip_length
        if callback:
            assert inspect.isfunction(callback), 'callback must be a function'
            assert len(inspect.getargspec(callback)[0]) == 1
        self.callback = callback
        self.rec_rate = rec_rate
        self.stereo = rec_stereo
        self.log = get_logger(self.__class__.__name__)
        self.silence = 'silence_stream://0'  # infinite silence stream
        self.iterations = iterations
        self.tail = 1.0
        self.call_count = 0
        # slave specific
        self.soundsdir = client.cmd('global_getvar sound_prefix')
        self.recsdir = client.cmd('global_getvar recordings_dir')
        self.audiofile = '{}/{}/{}/{}'.format(
            self.soundsdir, self.category, self.framerate, self.filename)
        self.call2recs = OrderedDict()
        self.host = client.host

        # self.stats = OrderedDict()

    def __setduration__(self, value):
        """Called when an originator changes it's `duration` attribute
        """
        self.iterations, self.tail = divmod(value, self.clip_length)
        if self.tail < 1.0:
            self.tail = 1.0

    def __setrate__(self, value):
        """Called when an originator changes it's `rate` attribute
        """
        # keep the rec rate at 1 rps during load testing
        # self.rec_rate = value

    @event_callback("CHANNEL_CREATE")
    def on_create(self, sess):
        """Mark all calls to NOT be hung up automatically by an `Originator`
        """
        if sess.is_outbound():
            sess.call.vars['noautohangup'] = True

    @event_callback("CHANNEL_PARK")
    def on_park(self, sess):
        if sess.is_inbound():
            sess.answer()

    @event_callback("CHANNEL_ANSWER")
    def on_answer(self, sess):
        call = sess.call
        if sess.is_inbound():
            self.call_count += 1

            # rec the callee stream
            count, rate = self.call_count, self.rec_rate
            # XXX could this be rate limit logic which determines the time
            # since last rec instead?
            if not (count % rate):
                filename = '{}/callee_{}.wav'.format(self.recsdir, sess.uuid)
                sess.start_record(filename, stereo=self.stereo)
                self.call2recs.setdefault(call.uuid, {})['callee'] = filename
                call.vars['record'] = True
                self.call_count = 0

            # set call length
            call.vars['iterations'] = self.iterations
            call.vars['tail'] = self.tail

        if sess.is_outbound():
            if call.vars.get('record'):  # call is already recording
                # rec the caller stream
                filename = '{}/caller_{}.wav'.format(self.recsdir, sess.uuid)
                sess.start_record(filename, stereo=self.stereo)
                self.call2recs.setdefault(call.uuid, {})['caller'] = filename
            else:
                self.trigger_playback(sess)

        # always enable a jitter buffer
        # sess.broadcast('jitterbuffer::60')

    @event_callback("PLAYBACK_START")
    def on_play(self, sess):
        fp = sess['Playback-File-Path']
        self.log.debug("Playing file '{}' for session '{}'"
                       .format(fp, sess.uuid))

        self.log.debug("fp is '{}'".format(fp))
        if fp == self.audiofile:
            sess.vars['clip'] = 'signal'
        elif fp == self.silence:
            # if playing silence tell the peer to start playing a signal
            sess.vars['clip'] = 'silence'
            peer = sess.call.get_peer(sess)
            peer.breakapp()
            peer.playback(self.audiofile)

    @event_callback("PLAYBACK_STOP")
    def on_stop(self, sess):
        '''On stop either trigger a new playing of the signal if more
        iterations are required or hangup the call.
        If the current call is being recorded schedule the recordings to stop
        and expect downstream callbacks to schedule call teardown.
        '''
        self.log.debug("Finished playing '{}' for session '{}'".format(
                      sess['Playback-File-Path'], sess.uuid))
        if sess.vars['clip'] == 'signal':
            vars = sess.call.vars
            vars['playback_count'] += 1

            if vars['playback_count'] < vars['iterations']:
                sess.playback(self.silence)
            else:
                # no more clips are expected to play
                if vars.get('record'):  # stop recording both ends
                    tail = vars['tail']
                    sess.stop_record(delay=tail)
                    peer = sess.call.get_peer(sess)
                    peer.breakapp()  # infinite silence must be manually killed
                    peer.stop_record(delay=tail)
                else:
                    # hangup calls not being recorded immediately
                    self.log.debug("sending hangup for session '{}'"
                                   .format(sess.uuid))
                    sess.sched_hangup(0.5)  # delay hangup slightly

    def trigger_playback(self, sess):
        '''Trigger clip playback on the given session by doing the following:
        1) Start playing a silence stream on the peer session
        2) This will in turn trigger a speech playback on this session in
           the "PLAYBACK_START" callback
        '''
        peer = sess.call.get_peer(sess)
        peer.playback(self.silence)  # play infinite silence
        peer.vars['clip'] = 'silence'
        # start counting number of clips played
        sess.call.vars['playback_count'] = 0

    @event_callback("RECORD_START")
    def on_rec(self, sess):
        self.log.debug("Recording file '{}' for session '{}'".format(
            sess['Record-File-Path'], sess.uuid)
        )
        # mark this session as "currently recording"
        sess.vars['recorded'] = False
        # sess.setvar('timer_name', 'soft')

        # start signal playback on the caller
        if sess.is_outbound():
            self.trigger_playback(sess)

    @event_callback("RECORD_STOP")
    def on_recstop(self, sess):
        self.log.debug("Finished recording file '{}' for session '{}'".format(
            sess['Record-File-Path'], sess.uuid))
        # mark as recorded so user can block with `EventListener.waitfor`
        sess.vars['recorded'] = True
        if sess.hungup:
            self.log.warn(
                "sess '{}' was already hungup prior to recording completion?"
                .format(sess.uuid))

        # if sess.call.vars.get('record'):
        #     self.stats[sess.uuid] = sess.con.api(
        #         'json {{"command": "mediaStats", "data": {{"uuid": "{0}"}}}}'
        #         .format(sess.uuid)
        #     ).getBody()

        # if the far end has finished recording then hangup the call
        if sess.call.get_peer(sess).vars.get('recorded', True):
            self.log.debug("sending hangup for session '{}'".format(sess.uuid))
            sess.sched_hangup(0.5)  # delay hangup slightly
            recs = self.call2recs[sess.call.uuid]

            # invoke callback for each recording
            if self.callback:
                self.callback(
                    RecInfo(self.host, recs['caller'], recs['callee'])
                )
