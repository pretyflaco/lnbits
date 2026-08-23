window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  data() {
    return {
      connectUri: '',
      sessionId: '',
      status: 'idle',
      failureReason: null,
      authUrl: null,
      pollTimer: null,
      alreadySignedIn: window.nostrloginAlreadySignedIn === true
    }
  },
  computed: {
    failureText() {
      return this.failureReason || 'Login failed. Please try again.'
    },
    failureNeedsLink() {
      return (this.failureReason || '')
        .toLowerCase()
        .includes('no account is linked')
    }
  },
  methods: {
    async startSession() {
      this.status = 'idle'
      this.failureReason = null
      this.authUrl = null
      this.connectUri = ''
      try {
        const {data} = await LNbits.api.request(
          'POST',
          '/nostrlogin/api/v1/session',
          null
        )
        this.sessionId = data.id
        this.connectUri = data.connect_uri
        this.status = 'pending'
        this.pollTimer = setInterval(() => this.pollStatus(), 2000)
      } catch (error) {
        this.status = 'failed'
        this.failureReason =
          error?.response?.data?.detail || 'Could not start a login session.'
      }
    },
    async pollStatus() {
      if (!this.sessionId) return
      try {
        const {data} = await LNbits.api.request(
          'GET',
          '/nostrlogin/api/v1/session/' + this.sessionId + '/status',
          null
        )
        this.status = data.status
        if (data.auth_url) this.authUrl = data.auth_url
        if (data.status === 'approved') {
          this.stopPolling()
          window.location.href = data.redirect || '/wallet'
        } else if (data.status === 'failed') {
          this.stopPolling()
          this.failureReason = data.reason || null
        }
      } catch (error) {
        this.stopPolling()
        this.status = 'failed'
        this.failureReason = 'Session expired. Please try again.'
      }
    },
    retry() {
      this.stopPolling()
      this.startSession()
    },
    copyUri() {
      LNbits.utils.copyText(this.connectUri)
    },
    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
      }
    }
  },
  created() {
    this.startSession()
  },
  beforeUnmount() {
    this.stopPolling()
  }
})
