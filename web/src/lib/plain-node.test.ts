import { describe, expect, it } from 'vitest';
import { accessDetail, auditWords, checkName, fixWords, localWhen, placeName, placeNames, plainDetail, plainReason, toolPlainTitle, workspaceWords } from './plain-node';

/** The walk's own sentences, as the node writes them, so a regression shows on the words a person reads. */
const HELD = 'Lane biz-b/social is not safe to use. The profile folder is missing, so the lane has no logins. Held for a human since 2026-11-07 15:21 UTC: needs_human:captcha.';
const NAMES = new Map([
  ['biz-a/shop', "Biz A's Shop"],
  ['biz-b/forum', "Biz B's Forum"],
]);

describe('plainDetail', () => {
  it('turns a hold into a plain sentence, the UTC time into the local time, and the raw reason into words', () => {
    const out = plainDetail(HELD);
    expect(out).not.toMatch(/UTC|needs_human|Held for a human/);
    expect(out).toContain('Held for a person since ');
    expect(out).toContain(', for a robot check.');
    expect(out).toContain(`since ${localWhen('2026-11-07T15:21:00Z')}`);
  });

  it('names an account by its workspace name when the names are known', () => {
    expect(plainDetail('Lane biz-a/shop is not safe to use.', NAMES)).toBe("Biz A's Shop is not safe to use.");
    expect(plainDetail('Lane biz-a/shop is not safe to use.')).toBe('Lane biz-a/shop is not safe to use.');
  });

  it('drops the commands from the sentences that carry them', () => {
    expect(plainDetail('4 lane(s) at format 1; 4 not stamped yet (agentos migrate run stamps them)')).toBe('4 accounts at format 1; 4 need updating (a migration updates them)');
    expect(plainDetail('4 lane(s); check each one with: agentos lane check --all')).toBe('4 accounts, and check each one on the computer');
    expect(plainDetail('no node.toml in home')).toBe('The settings file is missing');
    expect(plainDetail('no node.toml in /var/folders/x/T/agentos-dev')).toBe('The settings file is missing');
    expect(plainDetail('4 lane(s) at format 1')).toBe('4 accounts at format 1');
    expect(plainDetail('no backup in /var/folders/x/T/agentos-dev/backups; a dead disk would lose every login')).toBe('No backup is kept yet; a dead disk would lose every login');
    expect(plainDetail('99.1 GiB free of 926.4 GiB (11%) under /var/folders/x/T/agentos-dev')).toBe('99.1 GiB free of 926.4 GiB (11%) on this computer');
    expect(plainDetail('MCP answers on [address]:7761 with the node.toml token; requests without it get 401')).toBe("Agents reach this computer at this computer's address, and only with its key");
    expect(plainDetail('bubblewrap is missing (terminal and service lanes isolate programs with it)')).toBe("bubblewrap is missing. It keeps each environment's programs apart.");
    expect(plainDetail('8 problem(s): home is 755 (want 700); home/lanes is 755 (want 700) ...')).toBe('8 folders can be read by other users, so they should be private.');
  });

  it('leaves text no rule knows as the node wrote it', () => {
    expect(plainDetail('Running agentos 0.1.0.')).toBe('Running agentos 0.1.0.');
  });
});

describe('ids on the account screens', () => {
  it('says the profile folder check in words, whichever way the node wrote it', () => {
    expect(fixWords('Restore its profile folder from a backup, or set the account up again.').plain).toBe(
      'Restore its profile from a backup kept on this computer, or set the account up again.',
    );
  });

  it('says a profile restore in words, with the command under Technical details', () => {
    const restore = fixWords('restore home/lanes/biz-a/social/profile from a backup (agentos backup restore)');
    expect(restore.plain).toBe('Restore it from a backup kept on this computer.');
    expect(restore.command).toBe('restore home/lanes/biz-a/social/profile from a backup (agentos backup restore)');
    expect(restore.plain).not.toMatch(/home\/lanes|agentos/);
  });
});

describe('fixWords', () => {
  it('says what to do in a sentence and keeps the command for Technical details', () => {
    const backup = fixWords('agentos backup create --to /mnt/backup (or --to s3://bucket/agentos)');
    expect(backup.plain).toBe('Keep a backup on another disk, so one dead disk cannot take every login.');
    expect(backup.command).toBe('agentos backup create --to /mnt/backup (or --to s3://bucket/agentos)');
    expect(fixWords('AGENTOS_HOME=home agentos node init').plain).not.toMatch(/agentos|node init/);
    expect(fixWords('sudo apt-get install -y bubblewrap').plain).toMatch(/system programs/);
  });

  it('keeps a plain sentence the node wrote before its command', () => {
    const disk = fixWords('Free some disk, or keep the backups elsewhere: agentos backup create --to /mnt/backup');
    expect(disk.plain).toBe('Free some disk, or keep the backups elsewhere.');
    expect(disk.command).toBe('Free some disk, or keep the backups elsewhere: agentos backup create --to /mnt/backup');
  });

  it('falls back to a sentence that points at Technical details', () => {
    const odd = fixWords('do something nobody mapped');
    expect(odd.plain).toBe('Fix it on the computer. Technical details has the exact steps.');
    expect(odd.command).toBe('do something nobody mapped');
  });
});

describe('accessDetail', () => {
  it('reads an agent grant as who may act, up to what, and until when, without the grant id', () => {
    const out = accessDetail('agents use grant gr_pqyvim5xth3q7dvz (claude-code@alex-macbook-pro-3, up to irreversible), until 2027-02-04');
    expect(out).toMatch(/^Claude Code can act here, up to anything that cannot be undone, until .+2027\.$/);
    expect(out).not.toContain('gr_');
  });
});

describe('plainReason', () => {
  it('says what a failed push or refused step means', () => {
    expect(plainReason('ntfy unreachable: ConnectError')).toBe('The ntfy service could not be reached from this computer. Try again in a moment.');
    expect(plainReason('ntfy answered 503')).toBe('The ntfy service answered with an error (503).');
    expect(plainReason('webhook is not configured')).toBe('The webhook is not set up yet.');
  });

  it('says what a wrong passphrase and a busy account mean', () => {
    expect(plainReason('wrong passphrase')).toMatch(/did not open the file/);
    expect(plainReason('lane biz-a/social is warm; close it first')).toBe('This account is open right now. Close it first, then try again.');
  });

  it('says what an owner-only memory refusal and a bad site name mean', () => {
    expect(plainReason('only the owner (or an admin grant) may write owner-only entries')).toBe('Only the owner can change this.');
    expect(plainReason('domain must be a lowercase registrable domain like instagram.com')).toBe('Type the site by its plain address, such as instagram.com.');
  });

  it('says how to sign in without naming the command', () => {
    expect(plainReason('Sign in first. Run `agentos panel login` on this computer.')).toBe('Sign in again with a new code from this computer.');
    // The node's own sign-in words for a session that ended
    expect(plainReason('Sign in to the panel first. Run `agentos panel login` on the node for a sign-in link.')).toBe('Sign in again with a new code from this computer.');
  });
});

describe('auditWords', () => {
  it('reads an audit line as a sentence about the account', () => {
    expect(auditWords('panel panel.lane.check', 'biz-b/forum', NAMES)).toBe("Checked the account Biz B's Forum");
    expect(auditWords('panel panel.lane.check', 'biz-a/social')).toBe('Checked the account Social');
  });

  it('reads an unknown action in words, not as its id', () => {
    expect(auditWords('panel panel.thing.new', null)).toBe('Thing new');
  });
});

describe('names and labels', () => {
  it('names an account from its id', () => {
    expect(placeName('biz-a/shop', NAMES)).toBe("Biz A's Shop");
    expect(placeName('biz-c/social')).toBe('Social');
  });

  it('names the node checks a person would not say', () => {
    expect(checkName('Connection node-1')).toBe('Agent connection node-1');
    expect(checkName('Lanes')).toBe('Accounts');
    expect(checkName('Passes through')).toBe('Passes through');
  });
});

describe('words a computer says about its workers, environments and tools', () => {
  it('says what a worker cannot do without the engine words', () => {
    expect(plainReason('the Android engine does not exist yet, so this worker cannot run on any node')).toBe(
      'Android accounts are not supported yet, so this worker cannot run on any computer',
    );
    expect(plainReason('the desktop worker is planned for later: no desktop engine exists yet')).toBe(
      'Desktop apps are not supported yet, so this worker is not available here',
    );
    expect(plainReason('Browser accounts are built by agentos itself. Other browser engines and fonts are not here yet.')).toBe(
      'Browser accounts are built by agentos itself. Other browsers and fonts are not here yet.',
    );
  });

  it('titles each AI tool in everyday words, and keeps the node name for the rest', () => {
    expect(toolPlainTitle('login')).toBe('Log in with a saved login');
    expect(toolPlainTitle('env_create')).toBe('Make an environment');
    expect(toolPlainTitle('lane_exec')).toBe('Run a command');
    expect(toolPlainTitle('lane_install')).toBe('Install a program in an account');
    // A tool this build does not list still reads plainly, with a lane as an account.
    expect(toolPlainTitle('lane_new_thing')).toBe('Account new thing');
  });
});

describe('raw tool names and ids a person reads (2026-11-15 test, F7)', () => {
  const envNames = placeNames([{ name: 'Test', lanes: [{ id: 'test/linkedin', name: 'linkedin' }] }], [
    { title: 'qa-env-2026-11-15', lanes: { terminal: 'env/esrkyfqerme', browser: 'env/esrkyfqerme-browser' } },
  ]);

  it("reads an agent's tool in the audit log as what it does", () => {
    expect(auditWords('tool_end lane_close', 'test/linkedin', envNames)).toBe("Close an account: Test's LinkedIn");
    expect(auditWords('tool_end browser_act', 'test/linkedin', envNames)).toBe("Click or type on a page: Test's LinkedIn");
  });

  it('names an environment by its title, or says what it is', () => {
    expect(auditWords('panel panel.job.read', 'env/esrkyfqerme', envNames)).toBe('Read the output of a command in qa-env-2026-11-15');
    expect(auditWords('panel panel.job.read', 'env/esrkyfqerme')).toBe('Read the output of a command in an environment');
    expect(placeName('env/esrkyfqerme-browser', envNames)).toBe('the browser of qa-env-2026-11-15');
  });

  it('says every workspace for the star', () => {
    expect(workspaceWords('*')).toBe('every workspace');
    expect(workspaceWords('biz-a', 'Biz A')).toBe('Biz A');
  });

  it('reads the tools of an environment plainly', () => {
    expect(toolPlainTitle('lane_exec')).toBe('Run a command');
    expect(toolPlainTitle('ui_check')).toBe("Check a page's design");
    expect(toolPlainTitle('lane_request_control')).toBe('Ask for control back');
  });
});
