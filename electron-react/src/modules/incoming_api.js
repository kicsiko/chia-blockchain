export const Wallet = (id, name, type, data) => ({
  id: id,
  name: name,
  type: type,
  data: data,
  balance_total: 1,
  balance_pending: 1,
  transactions: [],
  puzzle_hash: ""
})

export const Transaction = (confirmed_at_index, created_at_time, to_puzzle_hash, amount,
  fee_amount, incoming, confirmed, sent, spend_bundle, additions, removals, wallet_id) => ({
  confirmed_at_index: confirmed_at_index,
  created_at_time: created_at_time,
  to_puzzle_hash: to_puzzle_hash,
  amount: amount,
  fee_amount: fee_amount,
  incoming: incoming,
  confirmed: confirmed,
  sent: sent,
  spend_bundle: spend_bundle,
  additions: additions,
  removals: removals,
  wallet_id: wallet_id
})

export const initial_wallet = Wallet(1, "Chia Wallet", "Standard Wallet", "")

const initial_state = { 
  mnemonic: [],
  logged_in: false,
  wallets: [,initial_wallet],
  presenting_wallet: 1,
};


export const incomingReducer = (state = { ...initial_state }, action) => {
  switch (action.type) {
    case "INCOMING_MESSAGE":
      if (action.command === "generate_mnemonic") {
        var mnemonic_data = action.data.mnemonic
        return { ...state, mnemonic: mnemonic_data};
      }
      else if (action.command === "log_in") {
        var success = action.data.success 
        return {...state, logged_in: success}
      }
      else if (action.command === "log_out") {
        var success = action.data.success 
        if (success) { 
          return {...state, logged_in: false}
        }
      }
      else if (action.command === "logged_in") {
        var logged_in = action.data.logged_in
        return {...state, logged_in: logged_in}
      }
      else if (action.command === "start_server") {
        var started = action.data.success
        return {...state, server_started: started}
      }
      else if (action.command === "get_wallets") {
        const wallets = action.data.wallets
        var wallets_state = []
        wallets.map((object) =>  {
            var id = parseInt(object.id)
            var wallet_obj = Wallet(id, object.name, object.type, object.data)
            wallets_state[id] = wallet_obj
        })
        console.log(wallets_state)
        return {...state, wallets: wallets_state}
      }
      else if (action.command === "get_wallet_balance") {
        if (action.data.success) {
          var id = action.data.wallet_id
          var wallets = state.wallets
          var wallet = wallets[parseInt(id)]
          wallet.balance = balance
          var balance = action.data.confirmed_wallet_balance
          console.log("balance is: " + balance)
          var unconfirmed_balance = action.data.unconfirmed_wallet_balance
          wallet.balance_total = balance
          wallet.balance_pending = unconfirmed_balance
          return {...state}
        }
      } 
      else if(action.command === "get_transactions") {
        var id = action.data.wallet_id
        var transactions = action.data.txs
        var wallets = state.wallets
        var wallet = wallets[parseInt(id)]
        wallet.transactions = transactions
        return {...state}
      } 
      else if (action.command === "get_next_puzzle_hash") {
        var id = action.data.wallet_id
        var puzzle_hash = action.data.puzzle_hash
        var wallets = state.wallets
        var wallet = wallets[parseInt(id)]
        console.log("wallet_id here: " + id)
        wallet.puzzle_hash = puzzle_hash
        return {...state}
      }
      return state
      break;
    default:
      return state;
  }
};
