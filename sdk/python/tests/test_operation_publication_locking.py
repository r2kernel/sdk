"""Cold Operation Event allocation must not hold the state/query mutex."""
import threading
import unittest
from r2kernel.core.subsystem_runtime import SubsystemRuntime, OperationRecord


class OperationPublicationLockTest(unittest.TestCase):
    def runtime(self):
        runtime=SubsystemRuntime.__new__(SubsystemRuntime)
        runtime._subsystem_name='source'
        runtime._timestamp_getter=lambda:100
        runtime._operation_lock=threading.RLock()
        runtime._operation_publish_topics_lock=threading.Lock()
        runtime._operation_publish_topics_registered=set()
        runtime._operation_contracts={}
        runtime._operation_records={}
        runtime.start_event_transport=lambda:None
        runtime.published=[]
        runtime.publish_event=lambda **args:runtime.published.append(args) or True
        runtime._operation_records['op']=self.record('op')
        return runtime

    def record(self,name):
        return OperationRecord(operation_id=name,operation='move_j',params={},created_ts=1,updated_ts=1)

    def publisher(self,runtime,release,entered,errors,state="succeeded"):
        def register(**args):
            if not entered.is_set():
                entered.set()
                if not release.wait(3):raise RuntimeError('test registration barrier was not released')
            return True
        runtime.register_event_publish=register
        def publish():
            try:runtime._publish_operation_state(operation_id='op',state=state)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=publish);thread.start()
        self.assertTrue(entered.wait(2))
        return thread

    def test_query_state_mutex_remains_available_during_cold_registration(self):
        runtime=self.runtime();release=threading.Event();entered=threading.Event();errors=[]
        thread=self.publisher(runtime,release,entered,errors)
        try:
            acquired=runtime._operation_lock.acquire(blocking=False)
            self.assertTrue(acquired,'cold Event registration holds the Operation query/state mutex')
            if acquired:runtime._operation_lock.release()
            response=runtime._build_operation_query_response(operation_id='op',request_id='query')
            self.assertTrue(response['ok']);self.assertEqual(response['state'],'queued')
        finally:
            release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])

    def test_feedback_and_result_allocation_do_not_hold_query_state_mutex(self):
        for kind in ('feedback','result'):
            with self.subTest(kind=kind):
                runtime=self.runtime();entered=threading.Event();release=threading.Event();errors=[]
                def register(**args):entered.set();release.wait(2);return True
                runtime.register_event_publish=register
                def publish():
                    try:getattr(runtime,'_publish_operation_'+kind)(operation_id='op',payload={'value':True})
                    except BaseException as error:errors.append(error)
                thread=threading.Thread(target=publish);thread.start();self.assertTrue(entered.wait(2))
                try:
                    acquired=runtime._operation_lock.acquire(blocking=False)
                    self.assertTrue(acquired)
                    if acquired:runtime._operation_lock.release()
                    self.assertTrue(runtime._build_operation_query_response(operation_id='op',request_id='query')['ok'])
                finally:
                    release.set();thread.join(2)
                self.assertFalse(thread.is_alive());self.assertEqual(errors,[])

    def test_cancellation_flag_and_query_are_observable_while_event_publication_waits(self):
        runtime=self.runtime();release=threading.Event();entered=threading.Event();errors=[]
        publisher=self.publisher(runtime,release,entered,errors,state='running')
        cancel_entered=threading.Event();responses=[]
        original=runtime._ensure_operation_publish_topics
        def ensure(operation):
            if threading.current_thread().name=='cancel-worker':cancel_entered.set()
            return original(operation)
        runtime._ensure_operation_publish_topics=ensure
        def cancel():
            try:responses.append(runtime._build_operation_cancel_response(operation_id='op',request_id='cancel',recv_ts=100))
            except BaseException as error:errors.append(error)
        cancel_thread=threading.Thread(target=cancel,name='cancel-worker');cancel_thread.start()
        try:
            self.assertTrue(cancel_entered.wait(2))
            self.assertTrue(runtime.is_operation_cancel_requested('op'))
            self.assertTrue(runtime._build_operation_query_response(operation_id='op',request_id='query')['cancel_requested'])
            self.assertEqual(responses,[]) # Existing cancel RPC waits for its state event.
        finally:
            release.set();publisher.join(2);cancel_thread.join(2)
        self.assertFalse(publisher.is_alive() or cancel_thread.is_alive());self.assertEqual(errors,[])
        self.assertTrue(responses[0]['ok']);self.assertTrue(responses[0]['cancel_requested'])
        self.assertEqual(runtime._operation_records['op'].state,'cancel_requested')
        self.assertEqual(len(runtime.published),1)

    def test_replaced_record_is_not_mutated_or_published_after_cold_registration(self):
        runtime=self.runtime();release=threading.Event();entered=threading.Event();errors=[]
        thread=self.publisher(runtime,release,entered,errors)
        try:
            with runtime._operation_lock:runtime._operation_records['op']=self.record('op')
        finally:
            release.set();thread.join(2)
        self.assertEqual(runtime._operation_records['op'].state,'queued')
        self.assertEqual(runtime.published,[]);self.assertEqual(errors,[])

    def test_two_operations_allocate_the_operation_topics_once(self):
        runtime=self.runtime();runtime._operation_records['other']=self.record('other');calls=[];errors=[]
        entered=threading.Event();release=threading.Event()
        def register(**args):
            calls.append(args['topic'])
            if len(calls)==1:
                entered.set();release.wait(2)
            return True
        runtime.register_event_publish=register
        def publish(name):
            try:runtime._publish_operation_state(operation_id=name,state='succeeded')
            except BaseException as error:errors.append(error)
        first=threading.Thread(target=publish,args=('op',));first.start();self.assertTrue(entered.wait(2))
        second=threading.Thread(target=publish,args=('other',));second.start();release.set()
        first.join(2);second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive());self.assertEqual(errors,[])
        self.assertEqual(len(calls),3);self.assertEqual(len(runtime.published),2)

    def test_cancelled_state_survives_a_delayed_running_publisher(self):
        runtime=self.runtime();runtime.register_event_publish=lambda **args:True
        prepared=threading.Event();resume=threading.Event();errors=[]
        original=runtime._prepare_operation_publication
        def prepare(operation_id):
            record=original(operation_id)
            if threading.current_thread().name=='delayed-running':
                prepared.set();resume.wait(2)
            return record
        runtime._prepare_operation_publication=prepare
        def publish():
            try:runtime._publish_operation_state(operation_id='op',state='running')
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=publish,name='delayed-running');thread.start();self.assertTrue(prepared.wait(2))
        try:
            response=runtime._build_operation_cancel_response(operation_id='op',request_id='cancel',recv_ts=100)
            self.assertTrue(response['cancel_requested'])
            self.assertEqual(runtime._operation_records['op'].state,'cancel_requested')
        finally:
            resume.set();thread.join(2)
        self.assertEqual(errors,[]);self.assertFalse(thread.is_alive())
        self.assertEqual(runtime._operation_records['op'].state,'cancel_requested')
        self.assertEqual(len(runtime.published),1)

    def test_warm_operation_feedback_and_result_do_not_wait_for_unrelated_cold_operation(self):
        runtime=self.runtime()
        runtime._operation_records['warm']=OperationRecord(operation_id='warm',operation='warm_operation',params={},created_ts=1,updated_ts=1)
        runtime._operation_publish_topics_registered.add('warm_operation')
        release=threading.Event();entered=threading.Event();errors=[];done=threading.Event()
        cold=self.publisher(runtime,release,entered,errors)
        def warm():
            try:
                runtime._publish_operation_feedback(operation_id='warm',payload={'value':True})
                runtime._publish_operation_result(operation_id='warm',payload={'value':True})
            except BaseException as error:errors.append(error)
            finally:done.set()
        thread=threading.Thread(target=warm);thread.start()
        try:self.assertTrue(done.wait(1),'warm operation waited for unrelated cold topic registration')
        finally:
            release.set();cold.join(2);thread.join(2)
        self.assertEqual(errors,[]);self.assertFalse(cold.is_alive() or thread.is_alive())
        self.assertEqual(len(runtime.published),3)

    def test_partial_registration_failure_is_explicit_and_releases_state_mutex(self):
        runtime=self.runtime();calls=[]
        def register(**args):calls.append(args['topic']);return len(calls)!=2
        runtime.register_event_publish=register
        with self.assertRaisesRegex(RuntimeError,'failed to allocate'):
            runtime._publish_operation_state(operation_id='op',state='succeeded')
        self.assertNotIn('move_j',runtime._operation_publish_topics_registered)
        self.assertEqual(runtime._build_operation_query_response(operation_id='op',request_id='query')['state'],'queued')
        self.assertEqual(runtime.published,[])

if __name__=='__main__':unittest.main()
