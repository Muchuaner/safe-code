"""Desktop 橋接協定檢查：真實角色/工具往返、續接、權限與失敗路徑。"""
import importlib.util
import http.client
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('gateway',Path(__file__).parents[1]/'src/SafeCode.Core/Remote/desktop_gateway.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)


def request(messages=None,**kwargs):
    return {'model':'claude-opus-5-5','max_tokens':2048,'messages':messages or [{'role':'user','content':'hello'}],**kwargs}


class FakeChannel:
    instances=[]
    def __init__(self,manager,data,directory=None,client_id=None):
        self.client_id=client_id;self.transient=data['_desktop_transient'];self.maximum=data['max_tokens'];self.sent=[];self.events=queue.Queue();self.closed=False
        self.directory=directory or Path(manager.directory)/'channel'
        self.instances.append(self)
    def send(self,event):
        self.sent.append(event)
        turn=event.get('turnId')
        if event['op']=='turn.start':
            self.events.put({'op':'turn.event','turnId':turn,'line':json.dumps({'type':'system','session_id':'session_12345678'})})
            tool=bool(event['spec']['tools'])
            content=[{'type':'tool_use','id':'native-tool-id','name':'mcp__codex__marker','input':{}}] if tool else [{'type':'text','text':'OK'}]
            self.events.put({'op':'turn.event','turnId':turn,'line':json.dumps({'type':'assistant','message':{'id':'msg_'+str(len(self.sent)),'content':content,'usage':{'input_tokens':100,'output_tokens':5}}})})
            if tool:self.events.put({'op':'turn.toolcall','turnId':turn,'callId':'external_call_123','name':'marker','args':{}})
            else:self.events.put({'op':'turn.exit','turnId':turn,'code':0})
        elif event['op']=='turn.toolresult':
            self.events.put({'op':'turn.event','turnId':turn,'line':json.dumps({'type':'assistant','message':{'id':'msg_tool_result','content':[{'type':'text','text':event['content'][0]['text']}],'usage':{'input_tokens':120,'output_tokens':7}}})})
            self.events.put({'op':'turn.exit','turnId':turn,'code':0})
    def next(self,*args):return self.events.get(timeout=1)
    def close(self):self.closed=True


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(g,'Channel',FakeChannel);self.patch.start();self.addCleanup(self.patch.stop)
        FakeChannel.instances=[];self.manager=g.Manager(self.temp.name,{'token':'local','instance':'instance'})
        self.addCleanup(self.manager.close)

    def test_cli_spec_permissions_and_model(self):
        response=self.manager.run(request(output_config={'effort':'high'}))
        self.assertEqual(response['content'],[{'type':'text','text':'OK'}])
        sent=FakeChannel.instances[0].sent[0]['spec']
        self.assertEqual(sent['model'],'claude-opus-5-5');self.assertEqual(sent['effort'],'high')
        self.assertEqual(sent['nativeTools'],'none');self.assertEqual(sent['permissionMode'],'dontAsk')
        self.assertNotIn('apiKey',sent)

    def test_catalog_uses_native_versions_efforts_and_additional_choices(self):
        home=Path(self.temp.name)
        (home/'.claude.json').write_text(json.dumps({'oauthAccount':{'secret':'not-exported'},'additionalModelOptionsCache':[
            {'value':'claude-fable-5-1[1m]','label':'Fable'},
            {'value':'opus[1m]','label':'Opus (1M)'},
            {'value':'https://untrusted.example','label':'invalid'}]}))
        rows=[{'value':'default','resolvedModel':'claude-opus-5-5','displayName':'Default'},
              {'value':'opus','resolvedModel':'claude-opus-5-5','displayName':'Opus','supportedEffortLevels':['low','high','max']},
              {'value':'fable','resolvedModel':'claude-fable-5-1','displayName':'Fable','supportedEffortLevels':['low','medium','high','xhigh','max']}]
        catalog=g.model_catalog(rows,home)
        self.assertEqual([m['id'] for m in catalog['data']],['claude-opus-5-5','claude-fable-5-1'])
        self.assertEqual(catalog['data'][0]['supported_effort_levels'],['low','high','max'])
        self.assertTrue(catalog['data'][1]['supports_1m'])
        self.assertTrue(catalog['data'][0]['supports_1m'])
        self.assertNotIn('not-exported',json.dumps(catalog))
        self.assertNotIn('untrusted',json.dumps(catalog))

    def test_model_switch_and_effort_reach_native_resume(self):
        messages=[{'role':'user','content':'hello'}]
        for model in ('claude-opus-5-5','claude-fable-5-1','claude-sonnet-5-5','claude-haiku-5-5'):
            response=self.manager.run(request(messages,model=model,output_config={'effort':'max'}))
            spec=FakeChannel.instances[-1].sent[0]['spec']
            self.assertEqual(spec['model'],model);self.assertEqual(spec['effort'],'max')
            if len(FakeChannel.instances)>1:self.assertEqual(spec['resume'],'session_12345678')
            messages += [{'role':'assistant','content':response['content']},{'role':'user','content':'continue'}]

    def test_1m_header_reaches_cli_without_changing_response_model(self):
        response=self.manager.run(request(model='claude-fable-5-1'),context_1m=True)
        self.assertEqual(FakeChannel.instances[0].sent[0]['spec']['model'],'claude-fable-5-1[1m]')
        self.assertEqual(response['model'],'claude-fable-5-1')
        self.manager.run(request(model='fable[1m]'),context_1m=True)
        self.assertEqual(FakeChannel.instances[-1].sent[0]['spec']['model'],'fable[1m]')

    def test_catalog_is_cached_and_does_not_start_inference_turn(self):
        expected={'data':[{'id':'claude-opus-5-5'}]}
        with patch.object(g,'native_model_catalog',return_value=expected) as discovery:
            self.assertEqual(self.manager.catalog(),expected)
            self.assertEqual(self.manager.catalog(),expected)
            discovery.assert_called_once_with()
        self.assertEqual(self.manager.started,0)
        self.assertFalse(FakeChannel.instances)

    def test_model_endpoint_requires_auth_and_reports_discovery_failure(self):
        server=g.Server(self.manager)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(token):
            client=http.client.HTTPConnection(*server.server_address,timeout=5)
            client.request('GET','/v1/models?limit=100',headers={'Authorization':'Bearer '+token})
            response=client.getresponse();status,body=response.status,json.loads(response.read());client.close()
            return status,body
        try:
            with patch.object(g,'native_model_catalog',return_value={'data':[{'id':'claude-opus-5-5'}]}) as discovery:
                self.assertEqual(get('wrong')[0],401);discovery.assert_not_called()
                self.assertEqual(get('local')[1]['data'][0]['id'],'claude-opus-5-5')
            self.manager.models=None
            with patch.object(g,'native_model_catalog',side_effect=g.GatewayError('private discovery details')):
                status,body=get('local');self.assertEqual(status,503)
                self.assertNotIn('private discovery details',json.dumps(body))
        finally:server.shutdown();server.server_close();thread.join()

    def test_tools_remain_pending_and_use_same_cli_turn(self):
        tools=[{'name':'marker','description':'test','input_schema':{'type':'object'}}]
        first=request(tools=tools);response=self.manager.run(first)
        self.assertEqual(response['stop_reason'],'tool_use')
        call=response['content'][-1];self.assertEqual(call['id'],'external_call_123');self.assertEqual(call['name'],'marker')
        messages=first['messages']+[{'role':'assistant','content':response['content']},{'role':'user','content':[{'type':'tool_result','tool_use_id':call['id'],'content':'marker-value'}]}]
        result=self.manager.run(request(messages,tools=tools))
        self.assertEqual(result['content'][0]['text'],'marker-value')
        channel=FakeChannel.instances[0]
        self.assertEqual([e['op'] for e in channel.sent],['turn.start','turn.toolresult'])
        self.assertEqual(len(FakeChannel.instances),1)
        self.assertEqual(self.manager.status()['usage']['input_tokens'],220)

    def test_desktop_tool_cannot_write_native_chat_cwd(self):
        class WrongPath(FakeChannel):
            def next(self, *args):
                event = super().next(*args)
                if event.get('op') == 'turn.toolcall':
                    event.update(name='Write',args={'file_path':str(g.desktop_project()/'test.txt')})
                return event

        with patch.object(g, 'Channel', WrongPath):
            data=request(tools=[{'name':'Write','input_schema':{'type':'object'}}])
            response=self.manager.run(data)
        self.assertTrue(g.targets_chat_storage({'name':'Bash','args':{
            'command':'mkdir -p "'+str(g.desktop_project())+'"'}}))
        self.assertFalse(g.targets_chat_storage({'name':'Bash','args':{'command':'pwd -W'}}))
        self.assertEqual(g.desktop_tool_input({'name':'Write','args':{
            'file_path':'/c/work/test.txt','content':'/d/data'}}),
            {'file_path':'C:/work/test.txt','content':'/d/data'})
        channel=FakeChannel.instances[-1]
        self.assertIn('pwd -W',channel.sent[0]['spec']['systemPrompt'])
        self.assertEqual(response['stop_reason'],'end_turn')
        self.assertTrue(channel.sent[1]['isError'])
        self.assertFalse(g.targets_chat_storage({'name':'Write','args':{
            'file_path':r'C:\work\test.txt','content':str(g.desktop_project())}}))

    def test_followup_resumes_native_session(self):
        first=request();response=self.manager.run(first)
        messages=first['messages']+[{'role':'assistant','content':response['content']},{'role':'user','content':'again'}]
        self.manager.run(request(messages))
        self.assertEqual(FakeChannel.instances[1].sent[0]['spec']['resume'],'session_12345678')
        self.assertEqual(FakeChannel.instances[0].directory,FakeChannel.instances[1].directory)
        self.assertTrue(FakeChannel.instances[0].closed)

    def test_identical_openers_resume_distinct_chats_after_restart(self):
        ids=['3b1fab1a-3b45-45e5-87b3-20dca78e1930','18ab101a-8566-41be-ba8e-c76e0be1480a']
        clients=[]
        for identity in ids:
            start=request(metadata={'user_id':json.dumps({'session_id':identity})})
            reply=self.manager.run(start,session_header=identity)
            clients.append(FakeChannel.instances[-1].client_id)
            self.assertEqual(self.manager.run(start,session_header=identity),reply)
        self.assertNotEqual(*clients)
        self.assertEqual(len(FakeChannel.instances),2)
        manager=g.Manager(self.temp.name,{'instance':'restarted'})
        self.addCleanup(manager.close)
        for i in (1,0):
            followup=request([{'role':'user','content':[{'type':'text','text':'<system-reminder>new context</system-reminder>'},
                                                        {'type':'text','text':'hello'}]},
                              {'role':'assistant','content':'OK'}, {'role':'user','content':str(i)}])
            manager.run(followup,session_header=ids[i])
            self.assertEqual(FakeChannel.instances[-1].client_id,clients[i])
            self.assertIn('resume',FakeChannel.instances[-1].sent[0]['spec'])

    def test_ambiguous_anonymous_history_and_edited_branches_are_isolated(self):
        for _ in range(2):self.manager.run(request())
        follow=request([{'role':'user','content':'hello'},{'role':'assistant','content':'OK'},
                        {'role':'user','content':'next'}])
        self.manager.run(follow)
        self.assertNotIn('resume',FakeChannel.instances[-1].sent[0]['spec'])
        identity='d575a868-d0b5-46a1-a604-b4258153f573'
        self.manager.run(request(),session_header=identity)
        follow['messages'][0]['content']='edited'
        self.manager.run(follow,session_header=identity)
        self.assertNotIn('resume',FakeChannel.instances[-1].sent[0]['spec'])

    def test_trimmed_history_retains_last_exchange_in_same_chat(self):
        identity='16b2d1af-f0be-45ac-93e7-eb41e242bb9b'
        self.manager.run(request(),session_header=identity)
        messages=request()['messages']+[{'role':'assistant','content':'OK'}, {'role':'user','content':'second'}]
        self.manager.run(request(messages),session_header=identity)
        trimmed=[messages[-1],{'role':'assistant','content':'OK'},{'role':'user','content':'third'}]
        self.manager.run(request(trimmed),session_header=identity)
        self.assertEqual(len({c.client_id for c in FakeChannel.instances}),1)

    def test_suggestion_keeps_original_checkpoint_after_restart(self):
        identity='16b2d1af-f0be-45ac-93e7-eb41e242bb9b'
        first=request()
        reply=self.manager.run(first,session_header=identity)
        original=FakeChannel.instances[-1]
        messages=first['messages']+[{'role':'assistant','content':reply['content']}]
        template=('[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]'
                  '\n\nReply with ONLY the suggestion, no quotes or explanation.')
        helper=request(messages+[{'role':'user','content':template}],
                       tools=[{'name':'marker','input_schema':{'type':'object'}}])
        self.manager.run(helper,session_header=identity)
        channel=FakeChannel.instances[-1]
        self.assertTrue(channel.transient)
        self.assertNotIn('resume',channel.sent[0]['spec'])
        self.assertFalse(channel.sent[0]['spec']['tools'])
        manager=g.Manager(self.temp.name,{'instance':'restart'})
        self.addCleanup(manager.close)
        manager.run(request(messages+[{'role':'user','content':'next'}]),session_header=identity)
        self.assertEqual(FakeChannel.instances[-1].client_id,original.client_id)
        self.assertIn('resume',FakeChannel.instances[-1].sent[0]['spec'])
        self.assertFalse(g.desktop_suggestion(messages+[{'role':'user','content':'What is suggestion mode?'}]))

    def test_only_sessionless_desktop_title_helper_is_transient(self):
        helper='You are coming up with a succinct title and git branch name for a coding session '+(
            'based on the provided description. Here is the session description:\n<description>hello</description>')
        data=request([{'role':'user','content':helper}])
        self.manager.run(data)
        self.assertTrue(FakeChannel.instances[-1].transient)
        self.manager.run(data,session_header='16b2d1af-f0be-45ac-93e7-eb41e242bb9b')
        self.assertFalse(FakeChannel.instances[-1].transient)
        self.assertEqual(g.desktop_title([{'role':'user','content':[
            {'type':'text','text':'<system-reminder>context</system-reminder>'},
            {'type':'text','text':'檢查  連線狀態'}]}]),'檢查 連線狀態')

    def test_webfetch_summary_has_separate_capacity_and_preserves_pending_turn(self):
        web_scope = '16b2d1af-f0be-45ac-93e7-eb41e242bb9b'
        other_scope = '469753af-cd73-40e1-8ead-d0634fe48726'
        web_tool = {'name': 'WebFetch', 'input_schema': {'type': 'object'}}
        other_tool = {'name': 'marker', 'input_schema': {'type': 'object'}}

        class WebFetchChannel(FakeChannel):
            def next(self, *args):
                event = super().next(*args)
                if event['op'] == 'turn.toolcall':
                    event['name'] = self.sent[0]['spec']['tools'][0]['name']
                return event

        with patch.object(g, 'Channel', WebFetchChannel):
            first = request(tools=[web_tool])
            reply = self.manager.run(first, session_header=web_scope)
            self.manager.run(request(tools=[other_tool]), session_header=other_scope)
            self.assertFalse(self.manager.live_slots.acquire(blocking=False))
            prompt = ('\nWeb page content:\n---\nFixture page\n---\n\nReport a marker.\n\n'
                      'Provide a concise response based only on the content above. In your response:\n'
                      ' - Never produce or reproduce exact song lyrics.')
            helper = request([{'role': 'user', 'content': prompt}], stream=True)
            for data, scope in ((helper, other_scope), (request(), web_scope)):
                with self.assertRaises(g.GatewayError) as error:
                    self.manager.run(data, session_header=scope)
                self.assertEqual(error.exception.status, 409)

            helper_response = self.manager.run(helper, session_header=web_scope)
            self.assertEqual(helper_response['stop_reason'], 'end_turn')
            summary_channel = FakeChannel.instances[-1]
            self.assertTrue(summary_channel.transient)
            self.assertNotIn('resume', summary_channel.sent[0]['spec'])
            self.assertEqual(summary_channel.sent[0]['spec']['tools'], [])
            self.assertEqual(len(self.manager.conversations), 2)
            self.assertTrue(self.manager.webfetch_slots.acquire(blocking=False))
            self.manager.webfetch_slots.release()

            outcome = {'type':'tool_result','tool_use_id':reply['content'][-1]['id'],'content':'marker-returned'}
            messages = first['messages'] + [
                {'role':'assistant','content':reply['content']}, {'role':'user','content':[outcome]},
            ]
            resumed = self.manager.run(request(messages, tools=[web_tool]), session_header=web_scope)
            self.assertEqual(resumed['content'][0]['text'], 'marker-returned')
            self.assertEqual([e['op'] for e in FakeChannel.instances[0].sent], ['turn.start', 'turn.toolresult'])

    def test_parallel_tools_wait_for_the_complete_streamed_batch(self):
        class ParallelChannel(FakeChannel):
            def send(self, frame):
                self.sent.append(frame)
                turn = frame['turnId']

                def emit(line):
                    self.events.put({'op': 'turn.event', 'turnId': turn, 'line': json.dumps(line)})

                def stream(kind, **fields):
                    emit({'type': 'stream_event', 'event': {'type': kind, **fields}})

                def tool_call(index):
                    self.events.put({'op': 'turn.toolcall', 'turnId': turn,
                                     'callId': f'bridge_{index}', 'toolUseId': f'native_{index}',
                                     'name': 'WebSearch', 'args': {'index': index}})

                if frame['op'] == 'turn.start':
                    stream('message_start', message={'id': 'msg_batch', 'usage': {}})
                    for index in range(4):
                        block = {'type': 'tool_use', 'id': f'native_{index}',
                                 'name': 'mcp__codex__WebSearch', 'input': {}}
                        stream('content_block_start', index=index, content_block=block)
                        emit({'type': 'assistant', 'message': {'id': 'msg_batch', 'content': [block]}})
                        if index == 0:
                            tool_call(0)
                    tool_call(2)
                    stream('message_delta', delta={'stop_reason': 'tool_use'})
                    stream('message_stop')
                    tool_call(1)
                    if not self.transient:
                        tool_call(3)
                elif sum(item['op'] == 'turn.toolresult' for item in self.sent) == 4:
                    emit({'type': 'assistant', 'message': {'id': 'msg_done',
                         'content': [{'type': 'text', 'text': 'all tools finished'}]}})
                    self.events.put({'op': 'turn.exit', 'turnId': turn, 'code': 0})

            def next(self, timeout):
                try:
                    return self.events.get_nowait()
                except queue.Empty:
                    raise g.GatewayError('VPS Claude Code timed out', 504) from None

        tools = [{'name': 'WebSearch', 'input_schema': {'type': 'object'}}]
        first = request(tools=tools)
        scope = '16b2d1af-f0be-45ac-93e7-eb41e242bb9b'
        with patch.object(g, 'Channel', ParallelChannel):
            reply = self.manager.run(first, session_header=scope)
            self.assertEqual(reply['stop_reason'], 'tool_use')
            self.assertEqual([block['id'] for block in reply['content']], [f'bridge_{i}' for i in range(4)])
            results = [{'type': 'tool_result', 'tool_use_id': block['id'], 'content': 'done'}
                       for block in reply['content']]
            history = first['messages'] + [
                {'role': 'assistant', 'content': reply['content']},
                {'role': 'user', 'content': results},
            ]
            self.assertEqual(self.manager.run(request(history, tools=tools), session_header=scope)['content'],
                             [{'type': 'text', 'text': 'all tools finished'}])
            with patch.object(g, 'auxiliary_request', return_value=True), self.assertRaises(g.GatewayError) as error:
                self.manager.run(first, session_header=scope)
            self.assertEqual(error.exception.status, 502)
            self.assertIn('complete tool call batch', str(error.exception))
            self.assertFalse(self.manager.conversations)

    def test_failed_admission_preserves_resume_pointer(self):
        data=request();self.manager.run(data)
        messages=data['messages']+[{'role':'assistant','content':'OK'},{'role':'user','content':'next'}]
        for _ in range(2):self.manager.live_slots.acquire()
        try:
            with self.assertRaises(g.GatewayError):self.manager.run(request(messages))
        finally:
            for _ in range(2):self.manager.live_slots.release()
        self.manager.run(request(messages))
        self.assertIn('resume',FakeChannel.instances[-1].sent[0]['spec'])

    def test_expired_tool_session_never_replays_tools(self):
        data=request([{'role':'user','content':[{'type':'tool_result','tool_use_id':'unknown','content':'secret'}]}])
        with self.assertRaises(g.GatewayError) as caught:self.manager.run(data)
        self.assertEqual(caught.exception.status,409);self.assertFalse(FakeChannel.instances)

    def test_no_direct_provider_and_reject_unsupported_controls(self):
        for changes in [{'model':'deepseek-flash'},{'temperature':.2},{'stop_sequences':['STOP']},{'tool_choice':{'type':'any'}},{'max_tokens':0}]:
            with self.subTest(changes=changes),self.assertRaises(g.GatewayError):self.manager.run(request(**changes))
        self.assertFalse(FakeChannel.instances)

    def test_http_keeps_cli_error_fields_across_assistant_and_result(self):
        class Rejected(FakeChannel):
            result_status = {}

            def send(self, event):
                details = {
                    'type': 'error',
                    'error': {'type': 'invalid_request_error', 'message': 'private request text'},
                    'request_id': 'req_fixture123',
                }
                assistant = {'type': 'assistant', 'isApiErrorMessage': True, 'message': {'content': [
                    {'type': 'text', 'text': 'API Error: 400 '+json.dumps(details)},
                ]}}
                result = {'type': 'result', 'is_error': True, **self.result_status}
                for line in (assistant, result):
                    self.events.put({'op': 'turn.event', 'turnId': event['turnId'], 'line': json.dumps(line)})

        with g.Server(self.manager) as server, patch.object(g, 'Channel', Rejected):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for result_status in ({}, {'api_error_status': 400}):
                    Rejected.result_status = result_status
                    client = http.client.HTTPConnection(*server.server_address, timeout=5)
                    try:
                        client.request('POST', '/v1/messages', json.dumps(request()), {
                            'Authorization': 'Bearer local', 'X-SafeCode-Diagnostics': '1',
                        })
                        response = client.getresponse()
                        self.assertEqual(response.status, 400)
                        report = json.loads(response.getheader(g.DIAGNOSTIC_HEADER))
                        self.assertEqual(report['stage'], 'anthropic_response')
                        self.assertEqual(report['anthropicStatus'], 400)
                        self.assertEqual(report['anthropicError'], 'invalid_request_error')
                        self.assertEqual(report['anthropicRequestId'], 'req_fixture123')
                        self.assertNotIn('private request text', json.dumps(report))
                        self.assertNotIn(b'private request text', response.read())
                    finally:
                        client.close()
            finally:
                server.shutdown()
                thread.join()

    def test_sse_preserves_content_and_usage(self):
        response=self.manager.run(request());events=list(g.sse_response(response))
        self.assertEqual(events[0][0],'message_start');self.assertEqual(events[-1][0],'message_stop')
        self.assertEqual(events[1][1]['content_block']['text'],'')
        self.assertEqual(events[2][1]['delta']['text'],'OK')
        self.assertEqual(events[-2][1]['usage']['output_tokens'],5)

    def test_usage_snapshots_not_double_counted(self):
        for value in [5,5,7,3]:self.manager.count_usage({'id':'same','usage':{'output_tokens':value}})
        self.assertEqual(self.manager.status()['usage']['output_tokens'],7)

    def test_final_cli_usage_replaces_partial_snapshots(self):
        self.manager.count_usage({'id':'first','usage':{'input_tokens':100,'output_tokens':0}},'turn')
        self.manager.count_usage({'id':'second','usage':{'input_tokens':110,'output_tokens':0}},'turn')
        self.manager.record_result('turn',{'input_tokens':210,'output_tokens':19})
        self.manager.record_result('turn',{'input_tokens':210,'output_tokens':19})
        self.assertEqual(self.manager.status()['usage']['input_tokens'],210)
        self.assertEqual(self.manager.status()['usage']['output_tokens'],19)

    def test_max_token_probe_returns_real_partial_response(self):
        class Truncated(FakeChannel):
            def send(self,event):
                self.sent.append(event)
                if event['op']!='turn.start':return
                turn=event['turnId']
                def line(value):self.events.put({'op':'turn.event','turnId':turn,'line':json.dumps(value)})
                for event in [
                    {'type':'message_start','message':{'id':'partial','usage':{'input_tokens':5}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'O'}},
                    {'type':'message_delta','delta':{'stop_reason':'max_tokens'},'usage':{'output_tokens':1}}]:line({'type':'stream_event','event':event})
                line({'type':'assistant','isApiErrorMessage':True,'error':'max_output_tokens','message':{'content':[{'type':'text','text':'synthetic CLI error'}]}})
                line({'type':'result','is_error':True,'usage':{'input_tokens':5,'output_tokens':1}})
                self.events.put({'op':'turn.exit','turnId':turn,'code':1})
        with patch.object(g,'Channel',Truncated):response=self.manager.run(request(max_tokens=1))
        self.assertEqual(response['stop_reason'],'max_tokens')
        self.assertEqual(response['content'],[{'type':'text','text':'O'}])
        self.assertEqual(response['usage']['output_tokens'],1)
        self.assertEqual(self.manager.status()['usage']['output_tokens'],1)

    def test_pending_turns_have_a_global_resource_limit(self):
        tools=[{'name':'marker','input_schema':{'type':'object'}}]
        self.manager.run(request(tools=tools));self.manager.run(request(tools=tools))
        with self.assertRaises(g.GatewayError) as caught:self.manager.run(request(tools=tools))
        self.assertEqual(caught.exception.status,429)
        self.assertEqual(len(FakeChannel.instances),2)

    def test_old_controller_cannot_stop_a_replacement_instance(self):
        directory=Path(self.temp.name)/'control';directory.mkdir()
        (directory/'component.json').write_text(json.dumps({'managedBy':'safe-code-desktop-vps'}))
        config=directory/'config.json';config.write_text(json.dumps({'instance':'new','token':'private'}))
        with patch('urllib.request.build_opener') as opener:
            result=g.manage(directory,{'action':'stop','instance':'old'})
            opener.return_value.open.assert_not_called()
        self.assertFalse(result['ready'])
        self.assertEqual(json.loads(config.read_text())['instance'],'new')

    def test_unknown_control_directory_is_not_overwritten(self):
        directory=Path(self.temp.name)/'unknown';directory.mkdir()
        file=directory/'user-data';file.write_text('keep')
        with self.assertRaises(g.GatewayError):g.manage(directory,{'action':'stop','instance':'x'})
        self.assertEqual(file.read_text(),'keep')

    def test_desktop_trailing_system_context_is_not_a_user_message(self):
        data=request([{'role':'user','content':'hello'},{'role':'system','content':'current desktop context'}],system='base instruction')
        response=self.manager.run(data)
        spec=FakeChannel.instances[0].sent[0]['spec']
        self.assertEqual(spec['systemPrompt'],'base instruction\ncurrent desktop context')
        self.assertEqual(spec['userMessage']['message']['content'],[{'type':'text','text':'hello'}])
        self.assertEqual(data['messages'][-1]['role'],'system')
        self.assertEqual(response['content'][0]['text'],'OK')


if __name__=='__main__':unittest.main()
