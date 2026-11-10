window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  data() {
    return {
      linkedPubkey: null,
      linkUri: '',
      linkSessionId: '',
      linkStatus: 'idle',
      linkError: null,
      authUrl: null,
      pollTimer: null,
      settingsOptions: [
        {
          name: 'relays',
          label: 'Relays',
          type: 'text',
          description: 'One relay websocket url per line'
        },
        {
          name: 'allow_auto_user_creation',
          label: 'Allow automatic account creation',
          type: 'bool',
          description: 'Create a new account when an unknown Nostr key signs in'
        },
        {
          name: 'sync_profile_pictures',
          label: 'Sync profile pictures',
          type: 'bool',
          description: 'Fetch the avatar of the linked Nostr profile on login'
        },
        {
          name: 'enable_diagnostic_logging',
          label: 'Diagnostic logging',
          type: 'bool',
          description: 'Verbose logging of handshake milestones'
        },
        {
          name: 'signer_app_image',
          label: 'Signer app image URL',
          description:
            'Avatar shown by the signer when pairing (nostrconnect image param)'
        }
      ]
    }
  },
  computed: {
    isAdmin() {
      return this.g.user?.admin === true || this.g.user?.super_user === true
    },
    adminKey() {
      const wallet = (this.g.user.wallets || [])[0]
      return wallet ? wallet.adminkey : null
    },
    linkedNpub() {
      if (!this.linkedPubkey) return ''
      try {
        return NostrTools.nip19.npubEncode(this.linkedPubkey)
      } catch (e) {
        return this.linkedPubkey
      }
    }
  },
  methods: {
    // Read the authoritative pubkey from the API; LNbits.map.user() drops it,
    // so g.user.pubkey is unreliable.
    async refreshLinkedKey() {
      try {
        const {data} = await LNbits.api.request('GET', '/api/v1/auth', null)
        this.linkedPubkey = data.pubkey || null
      } catch (error) {
        this.linkedPubkey = null
      }
    },
    async startLinkSession() {
      this.linkError = null
      try {
        const {data} = await LNbits.api.request(
          'POST',
          '/nostrlogin/api/v1/link/session',
          this.adminKey || this.g.user.wallets[0].inkey
        )
        this.linkSessionId = data.id
        this.linkUri = data.connect_uri
        this.linkStatus = 'pending'
        this.pollTimer = setInterval(() => this.pollLinkStatus(), 2000)
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    },
    async pollLinkStatus() {
      if (!this.linkSessionId) return
      try {
        const {data} = await LNbits.api.request(
          'GET',
          '/nostrlogin/api/v1/session/' + this.linkSessionId + '/status',
          null
        )
        this.linkStatus = data.status
        if (data.auth_url) this.authUrl = data.auth_url
        if (data.status === 'approved') {
          this.stopPolling()
          this.linkUri = ''
          this.authUrl = null
          Quasar.Notify.create({type: 'positive', message: 'Nostr key linked.'})
          await this.refreshLinkedKey()
        } else if (data.status === 'failed') {
          this.stopPolling()
          this.linkError = data.reason || 'Linking failed.'
          Quasar.Notify.create({
            type: 'negative',
            message: 'Linking failed.',
            caption: data.reason || ''
          })
        }
      } catch (error) {
        this.stopPolling()
        this.linkStatus = 'failed'
        this.linkError = 'Session expired. Please try again.'
      }
    },
    unlinkKey() {
      LNbits.utils
        .confirmDialog('Unlink your Nostr key from this account?')
        .onOk(async () => {
          try {
            await LNbits.api.request(
              'PUT',
              '/api/v1/auth/pubkey',
              this.g.user.wallets[0].adminkey,
              {user_id: this.g.user.id, pubkey: ''}
            )
            Quasar.Notify.create({
              type: 'positive',
              message: 'Nostr key unlinked.'
            })
            await this.refreshLinkedKey()
          } catch (error) {
            LNbits.utils.notifyApiError(error)
          }
        })
    },
    copyNpub() {
      LNbits.utils.copyText(this.linkedNpub)
    },
    copyLinkUri() {
      LNbits.utils.copyText(this.linkUri)
    },
    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
      }
    }
  },
  async created() {
    await this.refreshLinkedKey()
  },
  beforeUnmount() {
    this.stopPolling()
  }
})
