"""KPI-1 v2 counts accepted tasks without fabricating audible playback."""
import pytest
from hal.telemetry import voice_metrics as vm
from hal.drivers.voice._internal.turn_dispatch import _note_dispatch_outcome
from hal.drivers.voice._internal.sensing_sender import SendResult
from hal.test.test_voice_metrics import kpi, FakeTTS  # noqa: F401


@pytest.mark.parametrize('mode', ['turn', 'live'])
def test_delegate_no_reply_is_ack_not_audio_or_execution(kpi, mode):
    iid=vm.speech_end('test', at=kpi.clock(), mode=mode)
    vm.set_route(iid,'delegated','voice_followup')
    kpi.clock.advance(3000)
    vm.bind_run(iid,'main-run')
    _note_dispatch_outcome(iid,SendResult(run_id='main-run',delivered=True))
    kpi.close_all()
    row=kpi.one(vm.EVENT_INTERACTION)
    assert row['eligible'] and row['outcome']=='acknowledged'
    assert row['ack_schema_version']==2
    assert row['ack_latency_ms']==3000
    assert row['ack_kind']=='delegate_accepted'
    assert row['ack_modality']=='processing_accepted'
    assert row['audio_latency_ms'] is None and row['answer_latency_ms'] is None
    assert not kpi.of('voice_metrics_task_execution')


def test_audio_after_acceptance_keeps_separate_latency(kpi):
    iid=vm.speech_end('test');vm.bind_run(iid,'r')
    kpi.clock.advance(3000);vm.dispatch_accepted(iid)
    kpi.clock.advance(2800);vm.playback_audio('run:r',FakeTTS(realtime_feedback=True))
    kpi.close_all();row=kpi.one(vm.EVENT_INTERACTION)
    assert row['ack_latency_ms']==3000
    assert row['audio_latency_ms']==5800
    assert row['answer_latency_ms']==5800
    assert row['audio_kind']=='agent_reply'


def test_earlier_filler_wins_over_delegate(kpi):
    iid=vm.speech_end('test')
    kpi.clock.advance(500);vm.playback_audio('run:'+iid,FakeTTS(interruptible=True))
    kpi.clock.advance(1500);vm.dispatch_accepted(iid)
    kpi.close_all();row=kpi.one(vm.EVENT_INTERACTION)
    assert row['ack_latency_ms']==500 and row['ack_kind']=='waiting_audio'


@pytest.mark.parametrize('result', [SendResult(), SendResult(delivered=True), SendResult(run_id='r',delivered=False)])
def test_unconfirmed_dispatch_never_acks(kpi,result):
    iid=vm.speech_end('test');_note_dispatch_outcome(iid,result)
    kpi.close_all();row=kpi.one(vm.EVENT_INTERACTION)
    assert row['eligible'] and row['outcome']=='no_ack'


def test_mute_does_not_exclude_silent_local_task(kpi):
    iid=vm.speech_end('test');vm.playback_muted('run:'+iid)
    kpi.clock.advance(800)
    _note_dispatch_outcome(iid,SendResult(delivered=True,handled_locally=True))
    kpi.close_all();row=kpi.one(vm.EVENT_INTERACTION)
    assert row['eligible'] and row['speaker_muted']
    assert row['ack_latency_ms']==800 and row['ack_kind']=='local_accepted'
    assert row['audio_latency_ms'] is None


def test_late_receipt_and_audio_amend_without_replacing_first_ack(kpi):
    iid=vm.speech_end('test');vm._close_interaction(iid)
    kpi.clock.advance(11000);vm.dispatch_accepted(iid)
    kpi.clock.advance(1000);vm.playback_audio('run:'+iid,FakeTTS(realtime_feedback=True))
    rows=[x['params'] for x in kpi.of(vm.EVENT_INTERACTION)]
    assert [x['task_revision'] for x in rows]==[1,2,3]
    assert rows[0]['outcome']=='no_ack'
    assert rows[-1]['ack_latency_ms']==11000
    assert rows[-1]['audio_latency_ms']==12000


def test_missing_endpoint_remains_unknown_after_acceptance(kpi):
    endpoint=kpi.clock()
    iid=vm.speech_end('provider_transcript',endpoint_known=False,mode='live')
    kpi.clock.advance(1000);vm.dispatch_accepted(iid)
    vm._close_interaction(iid)
    row=kpi.one(vm.EVENT_INTERACTION)
    assert not row['eligible'] and row['ack_latency_ms'] is None
    vm.set_endpoint(iid,'real_endpoint',endpoint)
    row=kpi.of(vm.EVENT_INTERACTION)[-1]['params']
    assert row['eligible'] and row['ack_latency_ms']==1000
    assert row['audio_latency_ms'] is None
