"""Offline named raw dispatch preserves the verified request, without typed fallback."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from r2kernel.core.codec_lifecycle import CodecLifecycle
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.manifest import InterfaceDeclaration


class RawControlRegistrationTest(unittest.TestCase):
    def runtime(self):
        runtime = SubsystemRuntime.__new__(SubsystemRuntime)
        declaration = InterfaceDeclaration(name='private',contract='fixture.private.v1',visibility='internal',endpoints={'controls':('raw','mixed')})
        runtime._owner = SimpleNamespace(_subsystem_manifest=SimpleNamespace(provides={'private':declaration}))
        runtime._control_endpoint_handlers = {}
        runtime._raw_control_endpoint_handlers = {}
        runtime._control_contracts = {}
        runtime._procedure_handlers = {}
        runtime._typed_procedure_handlers = {}
        runtime._operation_handlers = {}
        runtime._procedure_handler_context = threading.local()
        runtime._control_handler = None
        runtime._control_server_enabled = True
        runtime._timestamp_getter = lambda: 100
        runtime._logger = SimpleNamespace(error=lambda *args: None)
        runtime._codec_lifecycle = CodecLifecycle(runtime)
        return runtime

    def test_full_raw_request_and_verified_authority_are_preserved(self):
        runtime = self.runtime(); observed=[]
        def handler(request,context):
            observed.append((request,context)); return {'ts':request['ts'],'status':0,'payload':request['payload']}
        runtime.register_raw_control_endpoint('raw',handler)
        authority={key:'value' for key in ('peer','epoch','source','source_session','source_registration','source_reference','target','target_session','target_registration','interface','binding_revision','topic')}
        authority.update(source='source.actual',topic='raw')
        request={'topic':'raw','ts':42,'payload':b'\x00\x01\x02\x03\x04\x05\x06\x07','sender':'__r2kernel_authority_v1__:'+json.dumps(authority),'_manager_peer_verified':True,'period_ms':20.,'deadline_ms':30.}
        response=runtime._dispatch_control_request(request)
        self.assertEqual(response,{'ts':42,'status':0,'payload':request['payload']})
        self.assertEqual(observed[0][0]['ts'],42)
        self.assertEqual(observed[0][0]['payload'],request['payload'])
        self.assertEqual(observed[0][0]['sender'],'source.actual')
        self.assertEqual(observed[0][1].source_authority,authority)
        self.assertEqual(runtime.get_control_info(),['raw'])
        self.assertEqual(runtime.get_control_contract_info()[0][1]['request_type'],'bytes')
        runtime._control_server_enabled=False
        self.assertEqual(runtime._dispatch_control_request(request)['status'],1)
        self.assertEqual(len(observed),1)

    def test_raw_replacement_and_typed_cross_plane_conflicts(self):
        runtime=self.runtime()
        runtime.register_raw_control_endpoint('raw',lambda request:{'ts':request['ts'],'status':0,'payload':b'old'})
        runtime.register_raw_control_endpoint('raw',lambda request:{'ts':request['ts'],'status':0,'payload':b'new'})
        self.assertEqual(runtime.get_control_info(),['raw'])
        self.assertEqual(runtime._dispatch_control_request({'topic':'raw','ts':7,'payload':b'12345678'})['payload'],b'new')
        with self.assertRaisesRegex(ValueError,'already registered as raw'):
            runtime.register_control_endpoint('raw',lambda payload:payload)
        with self.assertRaisesRegex(ValueError,'conflicts'):
            runtime._ensure_endpoint_plane_available('raw',plane='procedure')
        runtime.register_control_endpoint('mixed',lambda payload:{'ts':11,'status':0,'payload':payload})
        with self.assertRaisesRegex(ValueError,'already registered as typed'):
            runtime.register_raw_control_endpoint('mixed',lambda request:request)
        self.assertEqual(runtime.get_control_info(),['mixed','raw'])

    def test_private_declaration_and_typed_contract_are_required(self):
        runtime=self.runtime()
        with self.assertRaisesRegex(ValueError,'not a declared private'):
            runtime.register_raw_control_endpoint('unknown',lambda request:request)
        with self.assertRaisesRegex(ValueError,'SDK Protocol'):
            runtime.register_raw_control_endpoint('operation.query',lambda request:request)
        runtime._owner._subsystem_manifest.provides['private']=InterfaceDeclaration(name='private',contract='api.fixture.v1',visibility='internal',endpoints={'controls':('raw',)})
        api=SimpleNamespace(controls=[SimpleNamespace(topic='raw',request='msg.fixture.v1/Request',response='bytes')],qualify_type_ref=lambda ref:ref)
        with patch('r2kernel.core.codec_lifecycle.load_api_contract_spec',return_value=api):
            with self.assertRaisesRegex(ValueError,'cannot replace a typed'):
                runtime.register_raw_control_endpoint('raw',lambda request:request)
        self.assertEqual(runtime.get_control_info(),[])

    def test_raw_labels_and_reserved_protocol_families(self):
        for labels in ({'request_type':'bytes'}, {'request_type':'bytes','response_type':'msg.fixture.v1/Result'}, {'request_type':'msg.fixture.v1/Request','response_type':'bytes'}):
            runtime=self.runtime()
            runtime._owner._subsystem_manifest.provides['private']=InterfaceDeclaration(name='private',contract='fixture.private.v1',visibility='internal',endpoints={'controls':('raw',)},labels=labels)
            with self.assertRaisesRegex(ValueError,'type labels'):
                runtime.register_raw_control_endpoint('raw',lambda request:request)
            self.assertEqual(runtime.get_control_info(),[])
        runtime=self.runtime()
        runtime._owner._subsystem_manifest.provides['private']=InterfaceDeclaration(name='private',contract='fixture.private.v1',visibility='internal',endpoints={'controls':('raw',)},labels={'request_type':'bytes','response_type':'bytes'})
        runtime.register_raw_control_endpoint('raw',lambda request:request)
        for topic in ('procedure.private','operation.private'):
            with self.assertRaisesRegex(ValueError,'SDK Protocol'):
                runtime.register_raw_control_endpoint(topic,lambda request:request)

    def test_unverified_context_is_rejected_before_raw_callback(self):
        runtime=self.runtime();calls=[]
        runtime.register_raw_control_endpoint('raw',lambda request:calls.append(request) or {})
        with self.assertRaisesRegex(ValueError,'verified manager'):
            runtime._dispatch_control_request({'topic':'raw','ts':7,'payload':b'12345678','sender':'__r2kernel_authority_v1__:{}'})
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
