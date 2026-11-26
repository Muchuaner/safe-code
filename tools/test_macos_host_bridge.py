"""macOS UID proof for host aliases; no system SSH service is started."""
import importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('agent',root/'src/SafeCode.Core/Remote/agent.py');agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
class MacHostProof(unittest.TestCase):
 def test_darwin_uid_proof_and_mismatch(self):
  with tempfile.TemporaryDirectory() as tmp:
   directory=Path(tmp);client='b'*20;target=directory/'.ssh/safe-code-host'/client/'identity';target.parent.mkdir(parents=True);target.write_text('fixture-not-a-key')
   request={'operation':'host_bridge','relative':'.ssh/safe-code-host/'+client+'/identity','clientId':client,'action':'configure','alias':'mac','username':'tester','port':2048,'hostKey':'ssh-ed25519 AAAAfixture','userSid':'macos:501'}
   # Replace only subprocess.run in the isolated helper fixture to validate the exact commands and output branches.
   code="import subprocess,sys,json\noriginal=subprocess.run\ncommands=[]\ndef run(args,**kwargs):\n commands.append(args)\n text='tester\\n' if args[-1]=='whoami' else 'Darwin\\n501\\n'\n return subprocess.CompletedProcess(args,0,text,'')\nsubprocess.run=run\n"+agent.USER_FILES
   good=subprocess.run([sys.executable,'-I','-c',code],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(directory)})
   self.assertEqual(good.returncode,0,good.stderr);self.assertTrue(json.loads(good.stdout)['verified'])
   config=target.with_name('config');pin=target.with_name('known_hosts')
   original_config=(config.read_bytes(),config.stat().st_mtime_ns)
   original_pin=(pin.read_bytes(),pin.stat().st_mtime_ns)
   check={**request,'action':'verify'}
   proof=subprocess.run([sys.executable,'-I','-c',code],input=json.dumps(check),text=True,capture_output=True,env={**os.environ,'HOME':str(directory)})
   self.assertEqual(proof.returncode,0,proof.stderr);self.assertTrue(json.loads(proof.stdout)['verified'])
   self.assertEqual((config.read_bytes(),config.stat().st_mtime_ns),original_config)
   self.assertEqual((pin.read_bytes(),pin.stat().st_mtime_ns),original_pin)
   check['port']=2049
   wrong=subprocess.run([sys.executable,'-I','-c',code],input=json.dumps(check),text=True,capture_output=True,env={**os.environ,'HOME':str(directory)})
   self.assertNotEqual(wrong.returncode,0);self.assertIn('配置或指紋已變化',wrong.stderr)
   request['userSid']='macos:502'
   bad=subprocess.run([sys.executable,'-I','-c',code],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(directory)})
   self.assertNotEqual(bad.returncode,0);self.assertIn('macOS',bad.stderr)
   request['userSid']='macos:0'
   bad=subprocess.run([sys.executable,'-I','-c',code],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(directory)})
   self.assertNotEqual(bad.returncode,0)
if __name__=='__main__':unittest.main()
