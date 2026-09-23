import test from 'node:test';
import assert from 'node:assert/strict';
import {authorize} from '../store/auth.mjs';
const good={repository:'geniusgrok/trade-cli',repository_id:'1382522505',repository_owner:'geniusgrok',repository_owner_id:'332557824',ref:'refs/heads/main',sub:'repo:geniusgrok/trade-cli:ref:refs/heads/main',workflow_ref:'geniusgrok/trade-cli/.github/workflows/trade-daily.yml@refs/heads/main',runner_environment:'github-hosted',event_name:'schedule',run_id:'123',run_attempt:'1',sha:'a'.repeat(40)};
test('exact trusted workflow accepted',()=>assert.equal(authorize(good).run,'123'));
for(const [name, value] of Object.entries({repository:'other/repo',repository_id:'1',repository_owner_id:'1',ref:'refs/heads/evil',sub:'repo:geniusgrok/trade-cli:pull_request',workflow_ref:'geniusgrok/trade-cli/.github/workflows/evil.yml@refs/heads/main',runner_environment:'self-hosted',event_name:'pull_request',run_id:'123; sql',sha:'main'})) {
 test(`reject wrong ${name}`,()=>assert.throws(()=>authorize({...good,[name]:value})));
}
