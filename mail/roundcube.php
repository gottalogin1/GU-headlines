<?php
// Settings for the webmail (Roundcube). docker-compose.yml mounts this file
// into the webmail container; the mail server's address and ports are set
// there. Do not change this file on the server: scripts/auto-update.sh
// updates it.

// The name on the login page and in the browser tab.
$config['product_name'] = 'Webmail';

// Visitors arrive through Caddy, which says who they are and that they came
// over https. Only Caddy can reach the webmail (it has no port of its own),
// so the addresses of Docker's own network are trusted for this.
$config['proxy_whitelist'] = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'];
$config['use_https'] = true;

// At most 3 sign-in tries a minute for each visitor. (The mail server's
// Fail2Ban does not watch the webmail; see mail/fail2ban-jail.cf.)
$config['login_rate_limit'] = 3;

// One identity per mailbox, without changing its address: the mail server
// sends only as the mailbox's own address anyway (SPOOF_PROTECTION).
$config['identities_level'] = 1;

// No "Get support" link on the login page.
$config['support_url'] = '';
