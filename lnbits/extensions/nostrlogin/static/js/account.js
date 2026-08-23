window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  data() {
    return {
      linkUri: '',
      linkSessionId: '',
      linkStatus: 'idle',
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
          description:
            'Create a new account when an unknown Nostr key signs in'
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
        }
      ]
    }
  },
  computed: {
    adminKey() {
      const wallet = (this.g.user.wallets || [])[0]
      return wallet ? wallet.adminkey : null
    },
    linkedNpub() {
      if (!this.g.user.pubkey) return ''
      try {
        return window.NostrTools.nip19.npubEncode(this.g.user.pubkey)
      } catch (e) {
        return this.g.user.pubkey
      }
    }
  },
  methods: {
    async startLinkSession() {
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
          clearInterval(this.pollTimer)
          Quasar.Notify.create({type: 'positive', message: 'Key linked.'})
          setTimeout(() => window.location.reload(), 800)
        } else if (data.status === 'failed') {
          clearInterval(this.pollTimer)
          Quasar.Notify.create({
            type: 'negative',
            message: 'Linking failed.',
            caption: data.reason || ''
          })
        }
      } catch (error) {
        clearInterval(this.pollTimer)
        this.linkStatus = 'failed'
      }
    },
    unlinkKey() {
      LNbits.utils.confirmDialog('Unlink your Nostr key?').onOk(async () => {
        try {
          await LNbits.api.request(
            'PUT',
            '/api/v1/auth/pubkey',
            this.g.user.wallets[0].adminkey,
            {user_id: this.g.user.id, pubkey: ''}
          )
          Quasar.Notify.create({type: 'positive', message: 'Key unlinked.'})
          setTimeout(() => window.location.reload(), 800)
        } catch (error) {
          LNbits.utils.notifyApiError(error)
        }
      })
    }
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer)
  }
})
