//! RugGuard guard vault.
//!
//! "Give AI money without giving it the keys to everything": the vault holds the funds, the owner keeps
//! the full-access key, and the AI agent's account holds only a function-call key that can call
//! `request_swap` here. Function-call keys cannot attach deposits, so the agent cannot call
//! `ft_transfer_call` itself; every swap has to come through this contract, which checks:
//!
//! 1. the vault is not paused;
//! 2. RugGuard (the oracle account) has posted a score for the token, and it is fresh;
//! 3. the score is below the owner's threshold;
//! 4. the amount fits the per-swap limit and the daily cap.
//!
//! A refusal is a normal return (not a panic), so the decision and its reason stay on-chain as an event.
//! An approved swap is sent to the DEX by the vault itself: wNEAR `ft_transfer_call` with 1 yoctoNEAR,
//! carrying a Rhea (Ref) swap action. The callback refunds the daily allowance if the DEX refunded the input.

use near_sdk::json_types::U128;
use near_sdk::serde_json::json;
use near_sdk::store::LookupMap;
use near_sdk::{
    env, near, require, AccountId, BorshStorageKey, Gas, NearToken, PanicOnDefault, Promise, PromiseError,
    PromiseOrValue,
};

const NS_PER_DAY: u64 = 86_400_000_000_000;
const GAS_FT_TRANSFER_CALL: Gas = Gas::from_tgas(150);
const GAS_CALLBACK: Gas = Gas::from_tgas(15);

#[derive(BorshStorageKey)]
#[near]
enum StorageKey {
    Scores,
}

#[near(serializers = [borsh, json])]
#[derive(Clone)]
pub struct ScoreEntry {
    pub score: u8,
    /// Version of the scoring rules that produced the score, e.g. "score-v2".
    pub rules_version: String,
    /// Where the evidence lives (a report URL or hash), so a refusal can be traced.
    pub evidence: String,
    pub posted_at_ns: u64,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq)]
pub struct Decision {
    pub approved: bool,
    /// OK, PAUSED, NO_SCORE, STALE_SCORE, HIGH_RISK, OVER_SWAP_LIMIT, OVER_DAILY_CAP, SWAP_FAILED
    pub code: String,
    pub reason: String,
    pub token: AccountId,
    pub amount_in: U128,
    pub score: Option<u8>,
}

#[near(serializers = [json])]
pub struct Config {
    pub owner: AccountId,
    pub oracle: AccountId,
    pub agent: AccountId,
    pub dex: AccountId,
    pub wnear: AccountId,
    pub threshold: u8,
    pub max_per_swap: U128,
    pub daily_cap: U128,
    pub max_score_age_ns: u64,
    pub paused: bool,
    pub spent_today: U128,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct GuardVault {
    owner: AccountId,
    oracle: AccountId,
    agent: AccountId,
    dex: AccountId,
    wnear: AccountId,
    threshold: u8,
    max_per_swap: u128,
    daily_cap: u128,
    max_score_age_ns: u64,
    paused: bool,
    day: u64,
    spent_today: u128,
    scores: LookupMap<AccountId, ScoreEntry>,
}

fn event(name: &str, data: near_sdk::serde_json::Value) {
    env::log_str(&format!(
        "EVENT_JSON:{}",
        json!({"standard": "rugguard-guard", "version": "1.0.0", "event": name, "data": [data]})
    ));
}

#[near]
impl GuardVault {
    #[init]
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        owner: AccountId,
        oracle: AccountId,
        agent: AccountId,
        dex: AccountId,
        wnear: AccountId,
        threshold: u8,
        max_per_swap: U128,
        daily_cap: U128,
        max_score_age_hours: u64,
    ) -> Self {
        require!(threshold <= 100, "threshold must be 0-100");
        Self {
            owner,
            oracle,
            agent,
            dex,
            wnear,
            threshold,
            max_per_swap: max_per_swap.0,
            daily_cap: daily_cap.0,
            max_score_age_ns: max_score_age_hours * 3_600_000_000_000,
            paused: false,
            day: 0,
            spent_today: 0,
            scores: LookupMap::new(StorageKey::Scores),
        }
    }

    // ------------------------------------------------------------------ oracle

    /// RugGuard posts a score. Only the oracle account may call this.
    pub fn set_score(&mut self, token: AccountId, score: u8, rules_version: String, evidence: String) {
        require!(env::predecessor_account_id() == self.oracle, "only the oracle can post scores");
        require!(score <= 100, "score must be 0-100");
        require!(rules_version.len() <= 64 && evidence.len() <= 256, "rules_version or evidence too long");
        let entry = ScoreEntry { score, rules_version, evidence, posted_at_ns: env::block_timestamp() };
        event("score_posted", json!({"token": token, "score": score, "rules_version": entry.rules_version,
                                      "evidence": entry.evidence}));
        self.scores.insert(token, entry);
    }

    // ------------------------------------------------------------------ owner

    fn assert_owner(&self) {
        require!(env::predecessor_account_id() == self.owner, "only the owner can do this");
    }

    pub fn set_limits(&mut self, threshold: u8, max_per_swap: U128, daily_cap: U128, max_score_age_hours: u64) {
        self.assert_owner();
        require!(threshold <= 100, "threshold must be 0-100");
        self.threshold = threshold;
        self.max_per_swap = max_per_swap.0;
        self.daily_cap = daily_cap.0;
        self.max_score_age_ns = max_score_age_hours * 3_600_000_000_000;
        event("limits_set", json!({"threshold": threshold, "max_per_swap": max_per_swap,
                                    "daily_cap": daily_cap, "max_score_age_hours": max_score_age_hours}));
    }

    pub fn set_paused(&mut self, paused: bool) {
        self.assert_owner();
        self.paused = paused;
        event("paused_set", json!({"paused": paused}));
    }

    pub fn set_agent(&mut self, agent: AccountId) {
        self.assert_owner();
        self.agent = agent.clone();
        event("agent_set", json!({"agent": agent}));
    }

    pub fn set_oracle(&mut self, oracle: AccountId) {
        self.assert_owner();
        self.oracle = oracle.clone();
        event("oracle_set", json!({"oracle": oracle}));
    }

    // ------------------------------------------------------------------ agent

    /// The only method the agent's function-call key may call. Refusals return normally so the
    /// decision is recorded; an approved swap returns the DEX call, resolved by `on_swap_done`.
    pub fn request_swap(
        &mut self,
        token_out: AccountId,
        amount_in: U128,
        pool_id: u64,
        min_amount_out: U128,
    ) -> PromiseOrValue<Decision> {
        require!(env::predecessor_account_id() == self.agent, "only the agent can request swaps");
        self.roll_day();
        let decision = self.decide(&token_out, amount_in.0);
        event(if decision.approved { "swap_approved" } else { "swap_refused" }, json!(decision));
        if !decision.approved {
            return PromiseOrValue::Value(decision);
        }
        self.spent_today += amount_in.0;
        let msg = json!({
            "force": 0,
            "actions": [{
                "pool_id": pool_id,
                "token_in": self.wnear,
                "token_out": token_out,
                "amount_in": amount_in,
                "min_amount_out": min_amount_out,
            }]
        })
        .to_string();
        let args = json!({"receiver_id": self.dex, "amount": amount_in, "msg": msg}).to_string().into_bytes();
        PromiseOrValue::Promise(
            Promise::new(self.wnear.clone())
                .function_call("ft_transfer_call", args, NearToken::from_yoctonear(1), GAS_FT_TRANSFER_CALL)
                .then(
                    Self::ext(env::current_account_id())
                        .with_static_gas(GAS_CALLBACK)
                        .on_swap_done(decision),
                ),
        )
    }

    /// wNEAR's `ft_transfer_call` resolves to the amount the DEX kept. Anything refunded is given
    /// back to today's allowance.
    #[private]
    pub fn on_swap_done(&mut self, decision: Decision, #[callback_result] used: Result<U128, PromiseError>) -> Decision {
        let used = used.map(|u| u.0).unwrap_or(0);
        let refunded = decision.amount_in.0.saturating_sub(used);
        self.spent_today = self.spent_today.saturating_sub(refunded);
        if used == 0 {
            let failed = Decision {
                approved: false,
                code: "SWAP_FAILED".into(),
                reason: "The guard approved the swap but the DEX refunded the input.".into(),
                ..decision
            };
            event("swap_failed", json!(failed));
            return failed;
        }
        event("swap_done", json!({"token": decision.token, "amount_in": decision.amount_in, "used": U128(used)}));
        decision
    }

    // ------------------------------------------------------------------ views

    /// Dry run: what would happen if the agent asked now (does not change state).
    pub fn check(&self, token_out: AccountId, amount_in: U128) -> Decision {
        self.decide(&token_out, amount_in.0)
    }

    pub fn get_score(&self, token: AccountId) -> Option<ScoreEntry> {
        self.scores.get(&token).cloned()
    }

    pub fn get_config(&self) -> Config {
        let spent = self.effective_spent();
        Config {
            owner: self.owner.clone(),
            oracle: self.oracle.clone(),
            agent: self.agent.clone(),
            dex: self.dex.clone(),
            wnear: self.wnear.clone(),
            threshold: self.threshold,
            max_per_swap: U128(self.max_per_swap),
            daily_cap: U128(self.daily_cap),
            max_score_age_ns: self.max_score_age_ns,
            paused: self.paused,
            spent_today: U128(spent),
        }
    }
}

impl GuardVault {
    /// Today's spending; a new day starts at zero even before `roll_day` has stored it.
    fn effective_spent(&self) -> u128 {
        if env::block_timestamp() / NS_PER_DAY == self.day { self.spent_today } else { 0 }
    }

    fn roll_day(&mut self) {
        let today = env::block_timestamp() / NS_PER_DAY;
        if today != self.day {
            self.day = today;
            self.spent_today = 0;
        }
    }

    fn decide(&self, token: &AccountId, amount: u128) -> Decision {
        let entry = self.scores.get(token);
        let score = entry.map(|e| e.score);
        let d = |approved: bool, code: &str, reason: String| Decision {
            approved,
            code: code.into(),
            reason,
            token: token.clone(),
            amount_in: U128(amount),
            score,
        };
        if self.paused {
            return d(false, "PAUSED", "The owner has paused the vault.".into());
        }
        let Some(e) = entry else {
            return d(false, "NO_SCORE", "RugGuard has not scored this token; the guard fails closed.".into());
        };
        let age = env::block_timestamp().saturating_sub(e.posted_at_ns);
        if age > self.max_score_age_ns {
            return d(false, "STALE_SCORE",
                     format!("The score is {} h old; the limit is {} h.", age / 3_600_000_000_000,
                             self.max_score_age_ns / 3_600_000_000_000));
        }
        if e.score >= self.threshold {
            return d(false, "HIGH_RISK",
                     format!("RugGuard scored this token {}/100 ({}), at or above the threshold {}. Evidence: {}",
                             e.score, e.rules_version, self.threshold, e.evidence));
        }
        if amount > self.max_per_swap {
            return d(false, "OVER_SWAP_LIMIT", format!("{} is above the per-swap limit {}.", amount, self.max_per_swap));
        }
        if self.effective_spent() + amount > self.daily_cap {
            return d(false, "OVER_DAILY_CAP",
                     format!("Already spent {} today; {} more would pass the daily cap {}.",
                             self.effective_spent(), amount, self.daily_cap));
        }
        d(true, "OK", format!("Score {}/100 is below the threshold {}; within limits.", e.score, self.threshold))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{get_logs, VMContextBuilder};
    use near_sdk::testing_env;

    const NEAR: u128 = 10u128.pow(24);
    const HOUR: u64 = 3_600_000_000_000;

    fn acc(s: &str) -> AccountId {
        s.parse().unwrap()
    }

    fn ctx(predecessor: &str, now_ns: u64) {
        let mut b = VMContextBuilder::new();
        b.current_account_id(acc("vault.rugguard-demo.testnet"))
            .predecessor_account_id(acc(predecessor))
            .block_timestamp(now_ns);
        testing_env!(b.build());
    }

    fn vault() -> GuardVault {
        ctx("owner.testnet", 10 * NS_PER_DAY);
        GuardVault::new(acc("owner.testnet"), acc("oracle.testnet"), acc("agent.testnet"),
                        acc("ref-finance-101.testnet"), acc("wrap.testnet"), 60,
                        U128(5 * NEAR), U128(8 * NEAR), 24)
    }

    fn post(v: &mut GuardVault, token: &str, score: u8, at: u64) {
        ctx("oracle.testnet", at);
        v.set_score(acc(token), score, "score-v2".into(), "runs/eval-holdout.md".into());
    }

    fn ask(v: &mut GuardVault, token: &str, amount: u128, at: u64) -> PromiseOrValue<Decision> {
        ctx("agent.testnet", at);
        v.request_swap(acc(token), U128(amount), 7, U128(1))
    }

    fn refused(p: PromiseOrValue<Decision>) -> Decision {
        match p {
            PromiseOrValue::Value(d) => d,
            PromiseOrValue::Promise(_) => panic!("expected a refusal"),
        }
    }

    const T: u64 = 10 * NS_PER_DAY + HOUR;

    #[test]
    fn high_risk_token_is_refused_with_evidence() {
        let mut v = vault();
        post(&mut v, "bome.rug.testnet", 90, T);
        let d = refused(ask(&mut v, "bome.rug.testnet", NEAR, T));
        assert_eq!((d.approved, d.code.as_str(), d.score), (false, "HIGH_RISK", Some(90)));
        assert!(d.reason.contains("90/100") && d.reason.contains("runs/eval-holdout.md"));
        assert!(get_logs().iter().any(|l| l.contains("swap_refused")));
        assert_eq!(v.get_config().spent_today.0, 0);
    }

    #[test]
    fn unscored_token_fails_closed() {
        let mut v = vault();
        let d = refused(ask(&mut v, "unknown.testnet", NEAR, T));
        assert_eq!(d.code, "NO_SCORE");
    }

    #[test]
    fn stale_score_is_refused() {
        let mut v = vault();
        post(&mut v, "clean.testnet", 20, T);
        let d = refused(ask(&mut v, "clean.testnet", NEAR, T + 25 * HOUR));
        assert_eq!(d.code, "STALE_SCORE");
    }

    #[test]
    fn clean_token_within_limits_is_sent_to_the_dex() {
        let mut v = vault();
        post(&mut v, "clean.testnet", 20, T);
        assert!(matches!(ask(&mut v, "clean.testnet", 3 * NEAR, T), PromiseOrValue::Promise(_)));
        assert!(get_logs().iter().any(|l| l.contains("swap_approved")));
        assert_eq!(v.get_config().spent_today.0, 3 * NEAR);
    }

    #[test]
    fn per_swap_limit_and_daily_cap() {
        let mut v = vault();
        post(&mut v, "clean.testnet", 20, T);
        assert_eq!(refused(ask(&mut v, "clean.testnet", 6 * NEAR, T)).code, "OVER_SWAP_LIMIT");
        let _ = ask(&mut v, "clean.testnet", 5 * NEAR, T);
        assert_eq!(refused(ask(&mut v, "clean.testnet", 4 * NEAR, T)).code, "OVER_DAILY_CAP");
        // the next day the allowance resets (the score is re-posted to stay fresh)
        post(&mut v, "clean.testnet", 20, T + NS_PER_DAY);
        assert!(matches!(ask(&mut v, "clean.testnet", 4 * NEAR, T + NS_PER_DAY), PromiseOrValue::Promise(_)));
    }

    #[test]
    fn threshold_is_inclusive_and_owner_can_change_it() {
        let mut v = vault();
        post(&mut v, "edge.testnet", 60, T);
        assert_eq!(refused(ask(&mut v, "edge.testnet", NEAR, T)).code, "HIGH_RISK");
        ctx("owner.testnet", T);
        v.set_limits(70, U128(5 * NEAR), U128(8 * NEAR), 24);
        assert!(matches!(ask(&mut v, "edge.testnet", NEAR, T), PromiseOrValue::Promise(_)));
    }

    #[test]
    fn pause_blocks_everything() {
        let mut v = vault();
        post(&mut v, "clean.testnet", 20, T);
        ctx("owner.testnet", T);
        v.set_paused(true);
        assert_eq!(refused(ask(&mut v, "clean.testnet", NEAR, T)).code, "PAUSED");
    }

    #[test]
    fn refunded_swap_returns_the_allowance() {
        let mut v = vault();
        post(&mut v, "clean.testnet", 20, T);
        let _ = ask(&mut v, "clean.testnet", 3 * NEAR, T);
        let approved = v.check(acc("clean.testnet"), U128(3 * NEAR));
        ctx("vault.rugguard-demo.testnet", T);
        let d = v.on_swap_done(approved, Ok(U128(0)));
        assert_eq!(d.code, "SWAP_FAILED");
        assert_eq!(v.get_config().spent_today.0, 0);
    }

    #[test]
    fn check_is_a_dry_run() {
        let mut v = vault();
        post(&mut v, "bome.rug.testnet", 90, T);
        ctx("anyone.testnet", T);
        assert_eq!(v.check(acc("bome.rug.testnet"), U128(NEAR)).code, "HIGH_RISK");
        assert_eq!(v.get_config().spent_today.0, 0);
    }

    #[test]
    #[should_panic(expected = "only the agent")]
    fn others_cannot_request_swaps() {
        let mut v = vault();
        let _ = ask(&mut v, "x.testnet", NEAR, T);
        ctx("stranger.testnet", T);
        let _ = v.request_swap(acc("x.testnet"), U128(1), 1, U128(1));
    }

    #[test]
    #[should_panic(expected = "only the oracle")]
    fn only_the_oracle_posts_scores() {
        let mut v = vault();
        ctx("agent.testnet", T);
        v.set_score(acc("bome.rug.testnet"), 0, "fake".into(), "".into());
    }

    #[test]
    #[should_panic(expected = "only the owner")]
    fn agent_cannot_raise_its_own_limits() {
        let mut v = vault();
        ctx("agent.testnet", T);
        v.set_limits(100, U128(u128::MAX), U128(u128::MAX), 9999);
    }
}
