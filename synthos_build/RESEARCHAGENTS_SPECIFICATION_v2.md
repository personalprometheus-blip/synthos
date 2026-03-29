# RESEARCH AGENTS SPECIFICATION v2.0
## DisclosureResearchAgent (News) + SocialRumorAgent (Social/Rumor) Complete Logic

**Document Version:** 2.0
**Date:** March 28, 2026
**Status:** Design (not yet optimized for Pi placement)
**Scope:** Complete news research + social rumor analysis with 22 + 24 decision steps
**Model:** Built with full logic; optimization/splitting to retail/company later

---

## EXECUTIVE SUMMARY

The research layer consists of two parallel agents that feed into a common SentimentAgent:

**DisclosureResearchAgent** (`agent2_research.py`):
- Monitors news feeds from 50+ curated sources (retail) / 200+ master list (company)
- Classifies news by topic, entity, event type
- Extracts sentiment, novelty, impact, benchmark context
- Outputs scored signals: bullish/bearish/ignore/watch/benchmark-regime/alpha

**SocialRumorAgent** (new agent):
- Monitors social media for market-moving rumors
- Detects and scores unconfirmed claims
- Tracks source credibility, verification, bot activity
- Identifies manipulation (pump/dump, coordinated campaigns)
- Outputs similar signals but with confirmation/manipulation metrics

**Flow:**
```
News Sources (50 curated)
     ↓
DisclosureResearchAgent (22 steps)
     ↓
Scored news signals + metadata
     ↓
Social Media Sources
     ↓
SocialRumorAgent (24 steps)
     ↓
Scored rumor signals + metadata
     ↓
MarketSentimentAgent (combines, prioritizes holdings, sends to ExecutionAgent)
     ↓
ExecutionAgent (decides trade action)
```

Both agents produce the same signal types, enabling the SentimentAgent to cross-check and weight them.

---

## PART 1: DISCLOSURERESEARCHAGENT (22 Steps)

### 1. System Gate (News Feed Validation)

```
IF news feed unavailable
  → news_source_status != online
  → state = halt_ingestion

IF article parse failure
  → headline = null OR body_text = null
  → state = reject_article

IF timestamp missing
  → published_time = null
  → state = reject_article

IF timestamp stale
  → now - published_time > max_news_age (e.g., 45 days)
  → state = stale_article

IF duplicate article detected
  → semantic_similarity(article, stored_articles) > duplicate_threshold
  → state = suppress_duplicate
  → duplicate_counter += 1
  → duplicate_regions tracking updated

IF benchmark data unavailable
  → SPX_feed_status != online
  → state = benchmark_context_disabled

IF entity extraction failed
  → recognized_entities_count = 0 AND entity_required = true
  → state = low_context_article

IF article body too short
  → word_count < min_word_count (e.g., 50 words)
  → state = insufficient_content

IF unsupported language
  → language NOT IN supported_languages
  → state = reject_article
```

**Gate Output:**
- `article_status` ∈ {OK, HALT_FEED, REJECT, STALE, DUPLICATE, LOW_CONTEXT, INSUFFICIENT, LANGUAGE_ERROR}

---

### 2. Benchmark Gate (S&P 500 Context)

```
IF benchmark trend = up
  → MA_short(SPX) > MA_long(SPX)
  → benchmark_state = bullish

IF benchmark trend = down
  → MA_short(SPX) < MA_long(SPX)
  → benchmark_state = bearish

IF benchmark trend = flat
  → abs(MA_short(SPX) - MA_long(SPX)) <= trend_neutral_band
  → benchmark_state = neutral

IF benchmark volatility = high
  → realized_vol(SPX) > spx_vol_threshold OR VIX > vix_threshold
  → benchmark_vol_state = high

IF benchmark volatility = normal
  → realized_vol(SPX) <= spx_vol_threshold AND VIX <= vix_threshold
  → benchmark_vol_state = normal

IF benchmark drawdown active
  → (SPX_current - rolling_peak_SPX) / rolling_peak_SPX <= -drawdown_threshold
  → benchmark_risk_state = drawdown

IF benchmark momentum positive
  → ROC(SPX, lookback_n) > 0
  → benchmark_momentum_state = positive

IF benchmark momentum negative
  → ROC(SPX, lookback_n) < 0
  → benchmark_momentum_state = negative

IF benchmark sensitivity mode enabled
  → use_spx_context = true
  → news_scoring_mode = benchmark_adjusted
```

**Benchmark Output:**
- `benchmark_state` ∈ {BULLISH, BEARISH, NEUTRAL}
- `benchmark_vol_state` ∈ {LOW, NORMAL, HIGH}
- `benchmark_risk_state` ∈ {NORMAL, DRAWDOWN}
- `benchmark_momentum_state` ∈ {POSITIVE, NEGATIVE}

---

### 3. Source & Relevance Filter

```
IF source credibility below minimum
  → source_score < credibility_threshold
  → state = low_credibility

IF source credibility acceptable
  → source_score >= credibility_threshold
  → state = credible_source

IF article relevance below minimum
  → relevance_score < relevance_threshold
  → state = ignore_article

IF article relevance acceptable
  → relevance_score >= relevance_threshold
  → state = relevant_article

IF article is opinion-only and opinions excluded
  → content_type = opinion AND allow_opinion = false
  → state = reject_opinion

IF article is analysis and analysis allowed
  → content_type = analysis AND allow_analysis = true
  → state = keep_article

IF article outside allowed topic universe
  → topic NOT IN allowed_topics
  → state = out_of_scope

IF article within allowed topic universe
  → topic IN allowed_topics
  → state = in_scope
```

**Filter Output:**
- `source_credibility_state` ∈ {LOW, ACCEPTABLE, HIGH}
- `relevance_state` ∈ {RELEVANT, IRRELEVANT}
- `content_type_allowed` ∈ {True, False}

---

### 4. Topic Classification

```
IF article topic = macro
  → contains(rate_terms OR inflation_terms OR GDP_terms OR jobs_terms OR central_bank_terms)
  → topic_state = macro

IF article topic = earnings
  → contains(earnings_terms OR revenue_terms OR EPS_terms OR guidance_terms OR margin_terms)
  → topic_state = earnings

IF article topic = geopolitical
  → contains(war_terms OR sanctions_terms OR election_terms OR tariff_terms OR diplomacy_terms)
  → topic_state = geopolitical

IF article topic = regulatory
  → contains(SEC_terms OR DOJ_terms OR FTC_terms OR legislation_terms OR compliance_terms OR lawsuit_terms)
  → topic_state = regulatory

IF article topic = sector-specific
  → sector_entity_match = true AND broad_macro_match = false
  → topic_state = sector

IF article topic = company-specific
  → named_entity_recognition finds company_or_ticker
  → topic_state = company

IF article topic = market structure
  → contains(liquidity_terms OR exchange_terms OR market_making_terms OR clearing_terms)
  → topic_state = market_structure

IF article topic unclear
  → max(topic_probabilities) < topic_confidence_threshold
  → topic_state = uncertain
```

**Classification Output:**
- `topic_state` ∈ {MACRO, EARNINGS, GEOPOLITICAL, REGULATORY, SECTOR, COMPANY, MARKET_STRUCTURE, UNCERTAIN}

---

### 5. Entity Mapping

```
IF company entity detected
  → recognized_company_count >= 1
  → entity_state = company_linked

IF multiple companies detected
  → recognized_company_count > 1
  → entity_state = multi_company

IF sector entity detected
  → recognized_sector_count >= 1
  → entity_state = sector_linked

IF benchmark-linked entity detected
  → entity IN SPX_constituents OR entity_impact_scope = broad_market
  → entity_state = benchmark_relevant

IF no mapped tradeable entity
  → tradeable_entity_count = 0
  → entity_state = non_actionable
```

**Entity Output:**
- `entity_type` ∈ {COMPANY, MULTI_COMPANY, SECTOR, BENCHMARK, NON_ACTIONABLE}
- `identified_tickers` = [list of tickers]
- `identified_sectors` = [list of sectors]

---

### 6. Event Detection

```
IF scheduled event
  → published_time within event_window(known_calendar_events)
  → event_state = scheduled

IF unscheduled event
  → no_calendar_match = true AND event_detected = true
  → event_state = unscheduled

IF breaking news
  → source_breaking_flag = true OR article_burst_count > burst_threshold
  → event_state = breaking

IF follow-up article
  → semantic_similarity(article, active_event_cluster) > cluster_threshold
  → event_state = follow_up

IF rumor/unconfirmed
  → independent_confirmations < min_confirmations AND official_source_absent = true
  → event_state = rumor

IF official announcement
  → primary_source_present = true
  → event_state = official

IF revision/update article
  → contains(update_terms OR correction_terms OR revised_terms)
  → event_state = updated_event
```

**Event Output:**
- `event_type` ∈ {SCHEDULED, UNSCHEDULED, BREAKING, FOLLOW_UP, RUMOR, OFFICIAL, REVISION}

---

### 7. Sentiment Extraction

```
IF positive sentiment
  → sentiment_score > positive_threshold
  → sentiment_state = positive

IF negative sentiment
  → sentiment_score < negative_threshold
  → sentiment_state = negative

IF neutral sentiment
  → negative_threshold <= sentiment_score <= positive_threshold
  → sentiment_state = neutral

IF sentiment confidence low
  → sentiment_confidence < confidence_threshold
  → sentiment_state = uncertain

IF sentiment mixed
  → positive_term_density > mixed_floor AND negative_term_density > mixed_floor
  → sentiment_state = mixed

IF headline sentiment stronger than body sentiment
  → abs(headline_sentiment) - abs(body_sentiment) > headline_body_gap_threshold
  → sentiment_state = headline_exaggeration_flag
```

**Sentiment Output:**
- `sentiment_state` ∈ {POSITIVE, NEGATIVE, NEUTRAL, MIXED, UNCERTAIN}
- `sentiment_score` ∈ {-1.0 to +1.0}
- `sentiment_confidence` ∈ {0.0 to 1.0}

---

### 8. Surprise / Novelty

```
IF article contains positive surprise
  → actual_value - consensus_estimate > positive_surprise_threshold
  → surprise_state = positive

IF article contains negative surprise
  → actual_value - consensus_estimate < -negative_surprise_threshold
  → surprise_state = negative

IF novelty high
  → semantic_distance(article, prior_event_cluster) > novelty_threshold
  → novelty_state = high

IF novelty low
  → semantic_distance(article, prior_event_cluster) <= novelty_threshold
  → novelty_state = low

IF article already priced
  → pre_article_market_move_abs > priced_move_threshold AND novelty_state = low
  → pricing_state = already_priced

IF article adds incremental information
  → new_information_score >= min_incremental_info
  → pricing_state = incremental_update

IF repetition only
  → new_information_score < min_incremental_info
  → pricing_state = repetitive
```

**Novelty Output:**
- `novelty_state` ∈ {HIGH, LOW}
- `pricing_state` ∈ {ALREADY_PRICED, INCREMENTAL, REPETITIVE}
- `surprise_state` ∈ {POSITIVE, NEGATIVE, NONE}

---

### 9. Scope of Impact

```
IF impact scope = broad market
  → topic_state = macro OR event affects multiple sectors
  → impact_scope = marketwide

IF impact scope = sector
  → topic_state = sector AND sector_entity_match = true
  → impact_scope = sector_only

IF impact scope = company
  → topic_state = company AND recognized_company_count = 1
  → impact_scope = single_name

IF impact scope = peer group
  → recognized_company_count >= 1 AND peer_transfer_likelihood > peer_threshold
  → impact_scope = peer_group

IF impact scope unclear
  → scope_confidence < scope_threshold
  → impact_scope = unclear
```

**Scope Output:**
- `impact_scope` ∈ {MARKETWIDE, SECTOR, SINGLE_NAME, PEER_GROUP, UNCLEAR}

---

### 10. Time Horizon

```
IF impact horizon = immediate
  → historical_event_mapping(event_type).median_decay <= immediate_window
  → horizon_state = intraday

IF impact horizon = short
  → historical_event_mapping(event_type).median_decay > immediate_window AND <= short_window
  → horizon_state = multi_day

IF impact horizon = long
  → historical_event_mapping(event_type).median_decay > short_window
  → horizon_state = structural

IF event likely transient
  → persistence_score < transient_threshold
  → decay_state = fast_decay

IF event likely persistent
  → persistence_score >= persistent_threshold
  → decay_state = persistent
```

**Horizon Output:**
- `time_horizon` ∈ {INTRADAY, MULTI_DAY, STRUCTURAL}
- `expected_decay_rate` ∈ {FAST, MEDIUM, SLOW}

---

### 11. Benchmark-Relative Interpretation

```
IF news positive and benchmark bullish
  → sentiment_state = positive AND benchmark_state = bullish
  → relative_state = aligned_positive

IF news negative and benchmark bearish
  → sentiment_state = negative AND benchmark_state = bearish
  → relative_state = aligned_negative

IF news positive and benchmark bearish
  → sentiment_state = positive AND benchmark_state = bearish
  → relative_state = countertrend_positive

IF news negative and benchmark bullish
  → sentiment_state = negative AND benchmark_state = bullish
  → relative_state = countertrend_negative

IF article implies alpha opportunity
  → predicted_asset_move - predicted_SPX_move > alpha_threshold
  → signal_type = alpha

IF article implies beta move
  → abs(predicted_asset_move - predicted_SPX_move) <= beta_band
  → signal_type = beta

IF benchmark regime dominates article
  → benchmark_weight > article_specific_weight
  → interpretation_state = benchmark_dominant

IF article-specific effect dominates benchmark
  → article_specific_weight > benchmark_weight
  → interpretation_state = idiosyncratic_dominant
```

**Relative Output:**
- `relative_state` ∈ {ALIGNED_POSITIVE, ALIGNED_NEGATIVE, COUNTERTREND_POSITIVE, COUNTERTREND_NEGATIVE}
- `signal_type` ∈ {ALPHA, BETA}
- `interpretation_state` ∈ {BENCHMARK_DOMINANT, IDIOSYNCRATIC_DOMINANT}

---

### 12. Confirmation Controls

```
IF only one low-quality source
  → source_count = 1 AND source_score < credibility_threshold
  → confirmation_state = weak

IF multiple independent confirmations
  → independent_confirmations >= min_confirmations
  → confirmation_state = strong

IF primary source available
  → official_filing_present = true OR company_PR_present = true OR official_transcript_present = true
  → confirmation_state = primary_confirmed

IF contradiction detected
  → claim_variance_across_sources > contradiction_threshold
  → confirmation_state = contradictory

IF misinformation risk high
  → virality_score high AND verifiability_score low
  → confirmation_state = high_misinformation_risk

IF no confirmation after timeout
  → elapsed_time_since_first_article > confirmation_timeout AND independent_confirmations < min_confirmations
  → confirmation_state = expired_unconfirmed
```

**Confirmation Output:**
- `confirmation_state` ∈ {WEAK, STRONG, PRIMARY_CONFIRMED, CONTRADICTORY, HIGH_MISINFORMATION_RISK, EXPIRED_UNCONFIRMED}

---

### 13. Timing Controls

```
IF article published premarket
  → published_time < market_open
  → timing_state = premarket

IF article published intraday
  → market_open <= published_time <= market_close
  → timing_state = intraday

IF article published postmarket
  → published_time > market_close
  → timing_state = postmarket

IF article too old to act on
  → now - published_time > tradeable_news_window
  → timing_state = expired

IF dissemination delayed
  → major_source_lag > lag_threshold
  → timing_state = delayed_distribution

IF article burst ongoing
  → articles_per_topic_window > burst_threshold
  → timing_state = active_flow
```

**Timing Output:**
- `timing_state` ∈ {PREMARKET, INTRADAY, POSTMARKET, EXPIRED, DELAYED, ACTIVE_FLOW}

---

### 14. Crowding / Saturation

```
IF topic coverage high
  → topic_article_count(window_n) > coverage_threshold
  → crowding_state = crowded

IF public attention extreme
  → mention_count(news + social + search) > attention_threshold
  → crowding_state = extreme_attention

IF sentiment consensus crowded
  → sentiment_dispersion < low_dispersion_threshold
  → crowding_state = one_sided

IF event exhausted
  → realized_market_move_abs > exhaustion_move_threshold AND novelty_state = low
  → crowding_state = exhausted

IF event not exhausted
  → realized_market_move_abs <= exhaustion_move_threshold OR novelty_state = high
  → crowding_state = still_open
```

**Crowding Output:**
- `crowding_state` ∈ {CROWDED, EXTREME_ATTENTION, ONE_SIDED, EXHAUSTED, STILL_OPEN}

---

### 15. Contradiction / Ambiguity

```
IF article internally inconsistent
  → claim_conflict_score > internal_conflict_threshold
  → ambiguity_state = internally_conflicted

IF headline/body mismatch
  → abs(headline_sentiment - body_sentiment) > mismatch_threshold
  → ambiguity_state = headline_body_mismatch

IF uncertainty language high
  → uncertainty_term_density > uncertainty_threshold
  → ambiguity_state = uncertain_language

IF analyst interpretation split
  → expert_opinion_dispersion > split_threshold
  → ambiguity_state = externally_contested

IF ambiguity low
  → all ambiguity metrics <= thresholds
  → ambiguity_state = clear
```

**Ambiguity Output:**
- `ambiguity_state` ∈ {INTERNALLY_CONFLICTED, HEADLINE_BODY_MISMATCH, UNCERTAIN_LANGUAGE, EXTERNALLY_CONTESTED, CLEAR}

---

### 16. Impact Magnitude Estimation

```
IF expected impact magnitude = high
  → predicted_move_abs > high_impact_threshold
  → impact_state = high

IF expected impact magnitude = medium
  → medium_impact_threshold < predicted_move_abs <= high_impact_threshold
  → impact_state = medium

IF expected impact magnitude = low
  → predicted_move_abs <= medium_impact_threshold
  → impact_state = low

IF benchmark linkage high
  → historical_corr(event_class_response, SPX_response) > spx_link_threshold
  → impact_link_state = benchmark_linked

IF benchmark linkage low
  → historical_corr(event_class_response, SPX_response) <= spx_link_threshold
  → impact_link_state = benchmark_weak
```

**Magnitude Output:**
- `impact_magnitude` ∈ {HIGH, MEDIUM, LOW}
- `benchmark_linkage` ∈ {LINKED, WEAK}

---

### 17. Action Classification

```
IF positive + credible + novel + relevant
  → sentiment_state = positive AND source_score >= credibility_threshold AND novelty_state = high AND relevance_score >= relevance_threshold
  → classification = bullish_signal

IF negative + credible + novel + relevant
  → sentiment_state = negative AND source_score >= credibility_threshold AND novelty_state = high AND relevance_score >= relevance_threshold
  → classification = bearish_signal

IF low credibility
  → source_score < credibility_threshold
  → classification = ignore

IF low novelty
  → novelty_state = low AND pricing_state = already_priced
  → classification = ignore

IF mixed sentiment and high uncertainty
  → sentiment_state = mixed AND ambiguity_state != clear
  → classification = watch_only

IF broad macro and high benchmark linkage
  → topic_state = macro AND impact_link_state = benchmark_linked
  → classification = benchmark_regime_signal

IF company-specific and benchmark-neutral backdrop
  → topic_state = company AND benchmark_state = neutral AND signal_type = alpha
  → classification = relative_alpha_signal

IF rumor without confirmation
  → event_state = rumor AND confirmation_state != strong AND confirmation_state != primary_confirmed
  → classification = provisional_watch

IF contradiction high
  → confirmation_state = contradictory OR ambiguity_state = internally_conflicted
  → classification = freeze
```

**Classification Output:**
- `classification` ∈ {BULLISH_SIGNAL, BEARISH_SIGNAL, IGNORE, WATCH_ONLY, BENCHMARK_REGIME_SIGNAL, RELATIVE_ALPHA_SIGNAL, PROVISIONAL_WATCH, FREEZE}

---

### 18. Risk Discounts

```
IF sentiment confidence low
  → sentiment_confidence < confidence_threshold
  → impact_score = impact_score * low_confidence_discount

IF benchmark volatility high
  → benchmark_vol_state = high
  → impact_score = impact_score * high_vol_discount

IF event class historically noisy
  → historical_prediction_error(event_class) > noise_threshold
  → impact_score = impact_score * noise_discount

IF source reliability unstable
  → source_variance_score > reliability_variance_threshold
  → impact_score = impact_score * credibility_discount

IF contradictory updates arriving
  → new_conflicting_articles_count > conflict_update_threshold
  → classification = freeze
```

**Risk Adjustment Output:**
- `adjusted_impact_score` = impact_score * (discounts applied)
- `adjusted_classification` = updated after discounts

---

### 19. Persistence Controls

```
IF event is transient
  → persistence_score < transient_threshold
  → expected_decay = rapid

IF event is structural
  → persistence_score >= structural_threshold
  → expected_decay = slow

IF policy or regulation change
  → topic_state = regulatory AND legal_change_effective = true
  → expected_decay = structural

IF earnings beat/miss only
  → topic_state = earnings AND no_guidance_change = true
  → expected_decay = medium_or_fast

IF guidance revision
  → topic_state = earnings AND guidance_change_detected = true
  → expected_decay = persistent

IF geopolitical escalation
  → topic_state = geopolitical AND escalation_sequence = true
  → expected_decay = dynamically_updated
```

**Persistence Output:**
- `expected_decay_rate` ∈ {RAPID, MEDIUM, SLOW, STRUCTURAL, DYNAMIC}

---

### 20. Evaluation Loop

```
IF article retained in model
  → classification NOT IN {ignore, reject_article}
  → store article_state

IF prediction made
  → predicted_move exists
  → track realized_move over evaluation_window

IF prediction error high
  → abs(predicted_move - realized_move) > prediction_error_threshold
  → event_class_model_score = event_class_model_score - penalty

IF prediction error low
  → abs(predicted_move - realized_move) <= prediction_error_threshold
  → event_class_model_score = event_class_model_score + reward

IF repeated forecast error high
  → rolling_mean_prediction_error(event_class) > rolling_error_threshold
  → downweight event_class

IF repeated forecast accuracy high
  → rolling_mean_prediction_error(event_class) <= rolling_error_threshold
  → upweight event_class
```

**Evaluation Output:**
- `event_class_weight` = updated by performance
- `model_accuracy_by_event_class` = tracked

---

### 21. Output Controls

```
IF confidence high
  → overall_confidence >= high_confidence_threshold
  → output_mode = decisive

IF confidence medium
  → medium_confidence_threshold <= overall_confidence < high_confidence_threshold
  → output_mode = probabilistic

IF confidence low
  → overall_confidence < medium_confidence_threshold
  → output_mode = uncertain

IF benchmark context dominates
  → interpretation_state = benchmark_dominant
  → output_priority = benchmark_first

IF article-specific effect dominates
  → interpretation_state = idiosyncratic_dominant
  → output_priority = article_first

IF classification = freeze
  → classification == freeze
  → output_action = wait_for_confirmation

IF classification IN {bullish_signal, bearish_signal, benchmark_regime_signal, relative_alpha_signal}
  → output_action = emit_signal
```

**Output Control:**
- `output_mode` ∈ {DECISIVE, PROBABILISTIC, UNCERTAIN}
- `output_priority` ∈ {BENCHMARK_FIRST, ARTICLE_FIRST}
- `output_action` ∈ {EMIT_SIGNAL, WAIT_FOR_CONFIRMATION, SUPPRESS}

---

### 22. Final Composite Score

```
IF composite score calculated
  → composite_score = w1*credibility + w2*relevance + w3*novelty + w4*confirmation + w5*impact - w6*ambiguity - w7*crowding_discount
  → state = scored_article

IF composite score above bullish threshold
  → composite_score >= bullish_score_threshold AND sentiment_state = positive
  → final_signal = bullish

IF composite score below bearish threshold
  → composite_score <= bearish_score_threshold AND sentiment_state = negative
  → final_signal = bearish

IF composite score in neutral band
  → bearish_score_threshold < composite_score < bullish_score_threshold
  → final_signal = neutral_or_watch

IF benchmark override active
  → benchmark_risk_state = drawdown AND impact_link_state = benchmark_linked
  → final_signal = benchmark_override
```

**Final Output:**
```json
{
  "ticker": "AAPL",
  "headline": "...",
  "source": "Reuters",
  "published_time": "2026-03-28T10:30:00Z",
  "event_type": "earnings",
  "topic_state": "company",
  "sentiment_state": "positive",
  "sentiment_score": 0.75,
  "sentiment_confidence": 0.85,
  "novelty_state": "high",
  "surprise_state": "positive",
  "confirmation_state": "primary_confirmed",
  "impact_scope": "single_name",
  "impact_magnitude": "high",
  "time_horizon": "intraday",
  "expected_decay_rate": "medium",
  "relative_state": "aligned_positive",
  "signal_type": "beta",
  "interpretation_state": "idiosyncratic_dominant",
  "classification": "bullish_signal",
  "composite_score": 0.82,
  "final_signal": "bullish",
  "confidence": 0.82,
  "output_mode": "decisive",
  "duplicate_counter": 2,
  "duplicate_regions": ["regional", "global"],
  "staleness_value": 1.0,
  "ambiguity_state": "clear"
}
```

---

## PART 2: SOCIALRUMORAGENT (24 Steps)

### 1. System Gate (Social Feed Validation)

```
IF social feed unavailable
  → social_source_status != online
  → state = halt_ingestion

IF post parse failure
  → post_text = null
  → state = reject_post

IF timestamp missing
  → post_time = null
  → state = reject_post

IF timestamp stale
  → now - post_time > max_social_age
  → state = stale_post

IF duplicate post detected
  → semantic_similarity(post, stored_posts) > duplicate_threshold
  → state = suppress_duplicate

IF benchmark data unavailable
  → SPX_feed_status != online
  → state = benchmark_context_disabled

IF unsupported language
  → language NOT IN supported_languages
  → state = reject_post

IF post too short
  → character_count < min_character_count
  → state = insufficient_content
```

**Gate Output:**
- `post_status` ∈ {OK, HALT_FEED, REJECT, STALE, DUPLICATE, LOW_CONTEXT, INSUFFICIENT}

---

### 2. Source Identity Controls

```
IF source is verified
  → account_verification_flag = true
  → source_state = verified

IF source is unverified
  → account_verification_flag = false
  → source_state = unverified

IF source credibility high
  → source_score >= credibility_threshold
  → source_state = credible

IF source credibility low
  → source_score < credibility_threshold
  → source_state = low_credibility

IF source is official company/government account
  → account_type IN {company_official, regulator_official, government_official}
  → source_state = official

IF source is journalist / analyst / domain specialist
  → account_role IN {journalist, analyst, researcher, industry_specialist}
  → source_state = professional

IF source is anonymous
  → account_identity_confidence < identity_threshold
  → source_state = anonymous

IF source has prior rumor accuracy high
  → historical_claim_accuracy >= source_accuracy_threshold
  → source_state = historically_reliable

IF source has prior rumor accuracy low
  → historical_claim_accuracy < source_accuracy_threshold
  → source_state = historically_unreliable
```

**Source Output:**
- `source_type` ∈ {VERIFIED, UNVERIFIED, CREDIBLE, LOW_CREDIBILITY, OFFICIAL, PROFESSIONAL, ANONYMOUS, HISTORICALLY_RELIABLE, HISTORICALLY_UNRELIABLE}
- `source_score` ∈ {0.0 to 1.0}

---

### 3. Engagement / Propagation Controls

```
IF repost velocity high
  → reposts_per_minute > repost_velocity_threshold
  → propagation_state = fast_spread

IF like/comment velocity high
  → engagements_per_minute > engagement_velocity_threshold
  → propagation_state = high_attention

IF unique account propagation broad
  → unique_accounts_sharing > breadth_threshold
  → propagation_state = broad_distribution

IF propagation narrow
  → unique_accounts_sharing <= breadth_threshold
  → propagation_state = narrow_distribution

IF amplification concentrated
  → top_k_accounts_share_of_total_amplification > concentration_threshold
  → propagation_state = concentrated_boost

IF bot-like amplification detected
  → bot_probability(amplifying_accounts) > bot_threshold
  → propagation_state = synthetic_amplification

IF propagation decaying
  → engagement_velocity_t < engagement_velocity_t_minus_1 by decay_threshold
  → propagation_state = decaying
```

**Propagation Output:**
- `propagation_state` ∈ {FAST_SPREAD, HIGH_ATTENTION, BROAD_DISTRIBUTION, NARROW, CONCENTRATED, SYNTHETIC, DECAYING}
- `engagement_velocity` = engagements per minute
- `breadth_score` = unique accounts (normalized)

---

### 4. Content Classification

```
IF content is rumor
  → contains(unconfirmed_terms OR speculative_terms) AND official_source_absent = true
  → content_state = rumor

IF content is leak
  → contains(leak_terms OR insider_terms OR source_says_terms)
  → content_state = leak

IF content is reaction/opinion
  → contains(opinion_markers) AND factual_claim_density < fact_threshold
  → content_state = opinion

IF content is primary claim
  → claim_novelty > novelty_threshold AND claim_reference_count = 0
  → content_state = originating_claim

IF content is repost/commentary
  → references_existing_claim = true
  → content_state = secondary_flow

IF content is denial/refutation
  → contains(denial_terms OR false_terms OR inaccurate_terms)
  → content_state = refutation

IF content is satire/joke
  → satire_probability > satire_threshold
  → content_state = satire

IF content type unclear
  → max(content_type_probabilities) < content_confidence_threshold
  → content_state = uncertain
```

**Content Output:**
- `content_type` ∈ {RUMOR, LEAK, OPINION, PRIMARY_CLAIM, SECONDARY_FLOW, REFUTATION, SATIRE, UNCERTAIN}

---

### 5. Claim Detection

```
IF explicit market-moving claim detected
  → claim_contains(corporate_action OR earnings OR regulation OR macro_event OR geopolitical_event)
  → claim_state = market_relevant

IF claim references company
  → recognized_company_count >= 1
  → claim_scope = company

IF claim references sector
  → recognized_sector_count >= 1 AND recognized_company_count = 0
  → claim_scope = sector

IF claim references broad market
  → contains(index_terms OR macro_terms OR broad_policy_terms)
  → claim_scope = marketwide

IF no actionable claim detected
  → claim_extraction_confidence < claim_threshold
  → claim_state = non_actionable

IF multiple distinct claims detected
  → claim_count > 1
  → claim_state = multi_claim

IF claim specificity high
  → claim_has_named_entities AND claim_has_time_or_metric_detail
  → claim_state = specific

IF claim specificity low
  → claim_has_named_entities = false OR detail_density < detail_threshold
  → claim_state = vague
```

**Claim Output:**
- `claim_state` ∈ {MARKET_RELEVANT, NON_ACTIONABLE, MULTI_CLAIM}
- `claim_scope` ∈ {COMPANY, SECTOR, MARKETWIDE}
- `claim_specificity` ∈ {SPECIFIC, VAGUE}

---

### 6. Entity Mapping

```
IF company entity detected
  → recognized_company_count >= 1
  → entity_state = company_linked

IF company is in S&P 500
  → recognized_company IN SPX_constituents
  → entity_state = benchmark_constituent

IF multiple companies detected
  → recognized_company_count > 1
  → entity_state = multi_company

IF sector entity detected
  → recognized_sector_count >= 1
  → entity_state = sector_linked

IF regulator/government entity detected
  → recognized_regulatory_or_government_entity_count >= 1
  → entity_state = policy_linked

IF no mapped tradeable entity
  → tradeable_entity_count = 0
  → entity_state = non_actionable
```

**Entity Output:**
- `entity_type` ∈ {COMPANY_LINKED, BENCHMARK_CONSTITUENT, MULTI_COMPANY, SECTOR_LINKED, POLICY_LINKED, NON_ACTIONABLE}

---

### 7. Rumor Confirmation Controls

```
IF no independent confirmation
  → independent_confirmations = 0
  → confirmation_state = unconfirmed

IF one indirect confirmation
  → independent_confirmations = 1 AND primary_source_absent = true
  → confirmation_state = weak

IF multiple independent confirmations
  → independent_confirmations >= min_confirmations
  → confirmation_state = strong

IF primary source confirmation exists
  → official_filing_present = true OR official_statement_present = true OR primary_document_present = true
  → confirmation_state = primary_confirmed

IF primary source denial exists
  → official_denial_present = true
  → confirmation_state = officially_denied

IF contradiction across sources detected
  → claim_variance_across_sources > contradiction_threshold
  → confirmation_state = contradictory

IF confirmation timeout exceeded
  → elapsed_time_since_first_claim > confirmation_timeout AND confirmation_state IN {unconfirmed, weak}
  → confirmation_state = expired_unconfirmed
```

**Confirmation Output:**
- `confirmation_state` ∈ {UNCONFIRMED, WEAK, STRONG, PRIMARY_CONFIRMED, OFFICIALLY_DENIED, CONTRADICTORY, EXPIRED_UNCONFIRMED}
- `independent_confirmation_count` = integer

---

### 8. Sentiment Extraction

```
IF positive sentiment
  → sentiment_score > positive_threshold
  → sentiment_state = positive

IF negative sentiment
  → sentiment_score < negative_threshold
  → sentiment_state = negative

IF neutral sentiment
  → negative_threshold <= sentiment_score <= positive_threshold
  → sentiment_state = neutral

IF mixed sentiment
  → positive_term_density > mixed_floor AND negative_term_density > mixed_floor
  → sentiment_state = mixed

IF sentiment confidence low
  → sentiment_confidence < confidence_threshold
  → sentiment_state = uncertain

IF emotional intensity high
  → emotion_score > emotion_threshold
  → sentiment_state = emotionally_charged

IF outrage/panic language high
  → panic_term_density > panic_threshold
  → sentiment_state = panic_flag
```

**Sentiment Output:**
- `sentiment_state` ∈ {POSITIVE, NEGATIVE, NEUTRAL, MIXED, UNCERTAIN, EMOTIONALLY_CHARGED, PANIC_FLAG}
- `sentiment_score` ∈ {-1.0 to +1.0}
- `emotional_intensity` ∈ {0.0 to 1.0}

---

### 9. Novelty / Surprise Controls

```
IF rumor novelty high
  → semantic_distance(post, prior_claim_cluster) > novelty_threshold
  → novelty_state = high

IF rumor novelty low
  → semantic_distance(post, prior_claim_cluster) <= novelty_threshold
  → novelty_state = low

IF claim adds incremental information
  → new_information_score >= min_incremental_info
  → information_state = incremental

IF claim is repetitive
  → new_information_score < min_incremental_info
  → information_state = repetitive

IF rumor contains measurable surprise
  → actual_or_alleged_event_delta > surprise_threshold
  → surprise_state = high

IF rumor appears already priced
  → pre_post_market_move_abs > priced_move_threshold AND novelty_state = low
  → pricing_state = already_priced
```

**Novelty Output:**
- `novelty_state` ∈ {HIGH, LOW}
- `information_state` ∈ {INCREMENTAL, REPETITIVE}
- `surprise_state` ∈ {HIGH, LOW}
- `pricing_state` ∈ {ALREADY_PRICED, NOT_PRICED, PARTIALLY_PRICED}

---

### 10. Manipulation / Abuse Controls

```
IF pump language detected
  → contains(exaggerated_upside_terms OR coordinated_buy_terms)
  → abuse_state = pump_risk

IF dump language detected
  → contains(fear_trigger_terms OR coordinated_sell_terms)
  → abuse_state = dump_risk

IF coordination pattern detected
  → message_similarity(across_accounts) > coordination_threshold
  → abuse_state = coordinated_campaign

IF bot probability high
  → bot_probability(source_or_cluster) > bot_threshold
  → abuse_state = automation_risk

IF low-follower source with abnormal reach
  → follower_count < follower_floor AND propagation_state = fast_spread
  → abuse_state = suspicious_amplification

IF impersonation risk detected
  → identity_similarity_to_known_account > impersonation_threshold AND verification_flag = false
  → abuse_state = impersonation_risk

IF fabricated evidence risk detected
  → media_authenticity_score < authenticity_threshold
  → abuse_state = fabricated_media_risk
```

**Abuse Detection Output:**
- `abuse_state` ∈ {PUMP_RISK, DUMP_RISK, COORDINATED_CAMPAIGN, AUTOMATION_RISK, SUSPICIOUS_AMPLIFICATION, IMPERSONATION_RISK, FABRICATED_MEDIA_RISK, NONE}
- `abuse_probability` ∈ {0.0 to 1.0}

---

### 11. Media / Attachment Controls

```
IF screenshot/document attached
  → attachment_type IN {image, pdf, screenshot, document}
  → media_state = attached_evidence

IF attachment OCR or parse fails
  → attachment_parse_status != success
  → media_state = unreadable_attachment

IF attachment appears edited
  → forensic_edit_score > edit_threshold
  → media_state = manipulated_attachment_risk

IF attachment matches known official format
  → template_similarity(attachment, known_official_docs) > format_threshold
  → media_state = plausible_official_format

IF attachment provenance unknown
  → original_source_of_attachment = null
  → media_state = unknown_provenance

IF no attachment
  → attachment_count = 0
  → media_state = text_only
```

**Media Output:**
- `media_state` ∈ {ATTACHED_EVIDENCE, UNREADABLE, MANIPULATED_RISK, PLAUSIBLE_FORMAT, UNKNOWN_PROVENANCE, TEXT_ONLY}

---

### 12. Market Impact Scope

```
IF impact scope = single name
  → claim_scope = company AND recognized_company_count = 1
  → impact_scope = single_name

IF impact scope = peer group
  → peer_transfer_likelihood > peer_threshold
  → impact_scope = peer_group

IF impact scope = sector
  → claim_scope = sector
  → impact_scope = sector_only

IF impact scope = broad market
  → claim_scope = marketwide OR recognized_policy_entity = true
  → impact_scope = marketwide

IF scope unclear
  → scope_confidence < scope_threshold
  → impact_scope = unclear
```

**Scope Output:**
- `impact_scope` ∈ {SINGLE_NAME, PEER_GROUP, SECTOR, MARKETWIDE, UNCLEAR}

---

### 13. Time Horizon

```
IF horizon = immediate
  → historical_rumor_decay(event_class).median_decay <= immediate_window
  → horizon_state = intraday

IF horizon = short
  → historical_rumor_decay(event_class).median_decay > immediate_window AND <= short_window
  → horizon_state = multi_day

IF horizon = long
  → historical_rumor_decay(event_class).median_decay > short_window
  → horizon_state = persistent

IF rumor likely transient
  → persistence_score < transient_threshold
  → decay_state = fast_decay

IF rumor likely persistent
  → persistence_score >= persistent_threshold
  → decay_state = slow_decay
```

**Horizon Output:**
- `time_horizon` ∈ {INTRADAY, MULTI_DAY, PERSISTENT}
- `expected_decay_rate` ∈ {FAST, MEDIUM, SLOW}

---

### 14. Benchmark-Relative Interpretation

```
IF rumor positive and benchmark bullish
  → sentiment_state = positive AND benchmark_state = bullish
  → relative_state = aligned_positive

IF rumor negative and benchmark bearish
  → sentiment_state = negative AND benchmark_state = bearish
  → relative_state = aligned_negative

IF rumor positive and benchmark bearish
  → sentiment_state = positive AND benchmark_state = bearish
  → relative_state = countertrend_positive

IF rumor negative and benchmark bullish
  → sentiment_state = negative AND benchmark_state = bullish
  → relative_state = countertrend_negative

IF rumor implies alpha opportunity
  → predicted_asset_move - predicted_SPX_move > alpha_threshold
  → signal_type = alpha

IF rumor implies beta move
  → abs(predicted_asset_move - predicted_SPX_move) <= beta_band
  → signal_type = beta

IF benchmark dominates interpretation
  → benchmark_weight > rumor_specific_weight
  → interpretation_state = benchmark_dominant

IF rumor-specific effect dominates benchmark
  → rumor_specific_weight > benchmark_weight
  → interpretation_state = idiosyncratic_dominant
```

**Relative Output:**
- `relative_state` ∈ {ALIGNED_POSITIVE, ALIGNED_NEGATIVE, COUNTERTREND_POSITIVE, COUNTERTREND_NEGATIVE}
- `signal_type` ∈ {ALPHA, BETA}
- `interpretation_state` ∈ {BENCHMARK_DOMINANT, IDIOSYNCRATIC_DOMINANT}

---

### 15. Crowding / Saturation Controls

```
IF rumor cluster volume high
  → posts_in_claim_cluster(window_n) > cluster_volume_threshold
  → crowding_state = crowded

IF public attention extreme
  → mention_count(social + news + search) > attention_threshold
  → crowding_state = extreme_attention

IF sentiment consensus crowded
  → sentiment_dispersion < low_dispersion_threshold
  → crowding_state = one_sided

IF rumor exhausted
  → realized_market_move_abs > exhaustion_move_threshold AND novelty_state = low
  → crowding_state = exhausted

IF rumor still developing
  → cluster_growth_rate > growth_threshold
  → crowding_state = active_development
```

**Crowding Output:**
- `crowding_state` ∈ {CROWDED, EXTREME_ATTENTION, ONE_SIDED, EXHAUSTED, ACTIVE_DEVELOPMENT}

---

### 16. Contradiction / Ambiguity Controls

```
IF claim internally inconsistent
  → claim_conflict_score > internal_conflict_threshold
  → ambiguity_state = internally_conflicted

IF post hedged heavily
  → uncertainty_term_density > uncertainty_threshold
  → ambiguity_state = hedged_language

IF sarcasm probability high
  → sarcasm_score > sarcasm_threshold
  → ambiguity_state = sarcasm_risk

IF conflicting posts within cluster
  → cross_post_claim_variance > cluster_conflict_threshold
  → ambiguity_state = cluster_conflicted

IF ambiguity low
  → all ambiguity metrics <= thresholds
  → ambiguity_state = clear
```

**Ambiguity Output:**
- `ambiguity_state` ∈ {INTERNALLY_CONFLICTED, HEDGED_LANGUAGE, SARCASM_RISK, CLUSTER_CONFLICTED, CLEAR}

---

### 17. Timing Controls

```
IF post published premarket
  → post_time < market_open
  → timing_state = premarket

IF post published intraday
  → market_open <= post_time <= market_close
  → timing_state = intraday

IF post published postmarket
  → post_time > market_close
  → timing_state = postmarket

IF post too old to act on
  → now - post_time > tradeable_social_window
  → timing_state = expired

IF rumor burst ongoing
  → posts_per_minute_in_cluster > burst_threshold
  → timing_state = active_flow

IF rumor flow decaying
  → posts_per_minute_in_cluster < decay_flow_threshold
  → timing_state = fading
```

**Timing Output:**
- `timing_state` ∈ {PREMARKET, INTRADAY, POSTMARKET, EXPIRED, ACTIVE_FLOW, FADING}

---

### 18. Impact Magnitude Estimation

```
IF expected impact magnitude = high
  → predicted_move_abs > high_impact_threshold
  → impact_state = high

IF expected impact magnitude = medium
  → medium_impact_threshold < predicted_move_abs <= high_impact_threshold
  → impact_state = medium

IF expected impact magnitude = low
  → predicted_move_abs <= medium_impact_threshold
  → impact_state = low

IF benchmark linkage high
  → historical_corr(rumor_class_response, SPX_response) > spx_link_threshold
  → impact_link_state = benchmark_linked

IF benchmark linkage low
  → historical_corr(rumor_class_response, SPX_response) <= spx_link_threshold
  → impact_link_state = benchmark_weak
```

**Magnitude Output:**
- `impact_magnitude` ∈ {HIGH, MEDIUM, LOW}
- `benchmark_linkage` ∈ {LINKED, WEAK}

---

### 19. Action Classification

```
IF credible + novel + relevant + positive
  → source_score >= credibility_threshold AND novelty_state = high AND claim_state = market_relevant AND sentiment_state = positive
  → classification = bullish_rumor_signal

IF credible + novel + relevant + negative
  → source_score >= credibility_threshold AND novelty_state = high AND claim_state = market_relevant AND sentiment_state = negative
  → classification = bearish_rumor_signal

IF low credibility and no confirmation
  → source_score < credibility_threshold AND confirmation_state IN {unconfirmed, weak}
  → classification = ignore

IF manipulation risk high
  → abuse_state IN {pump_risk, dump_risk, coordinated_campaign, automation_risk, impersonation_risk, fabricated_media_risk}
  → classification = manipulation_watch

IF mixed sentiment and high ambiguity
  → sentiment_state = mixed AND ambiguity_state != clear
  → classification = watch_only

IF rumor broad macro and high benchmark linkage
  → impact_scope = marketwide AND impact_link_state = benchmark_linked
  → classification = benchmark_regime_signal

IF company-specific with benchmark-neutral backdrop
  → impact_scope = single_name AND benchmark_state = neutral AND signal_type = alpha
  → classification = relative_alpha_signal

IF officially denied
  → confirmation_state = officially_denied
  → classification = deny_and_discount

IF contradictory cluster
  → confirmation_state = contradictory OR ambiguity_state = cluster_conflicted
  → classification = freeze

IF primary confirmed rumor
  → confirmation_state = primary_confirmed AND claim_state = market_relevant
  → classification = upgraded_to_confirmed_event
```

**Classification Output:**
- `classification` ∈ {BULLISH_RUMOR_SIGNAL, BEARISH_RUMOR_SIGNAL, IGNORE, MANIPULATION_WATCH, WATCH_ONLY, BENCHMARK_REGIME_SIGNAL, RELATIVE_ALPHA_SIGNAL, DENY_AND_DISCOUNT, FREEZE, UPGRADED_TO_CONFIRMED_EVENT}

---

### 20. Risk Discounts

```
IF sentiment confidence low
  → sentiment_confidence < confidence_threshold
  → impact_score = impact_score * low_confidence_discount

IF source anonymous
  → source_state = anonymous
  → impact_score = impact_score * anonymity_discount

IF benchmark volatility high
  → benchmark_vol_state = high
  → impact_score = impact_score * high_vol_discount

IF manipulation risk present
  → classification = manipulation_watch OR abuse_state != null
  → impact_score = impact_score * manipulation_discount

IF contradictory updates arriving
  → new_conflicting_posts_count > conflict_update_threshold
  → classification = freeze

IF bot amplification high
  → propagation_state = synthetic_amplification
  → impact_score = impact_score * bot_discount
```

**Risk Adjustment Output:**
- `adjusted_impact_score` = impact_score * (discounts applied)

---

### 21. Persistence Controls

```
IF rumor is transient
  → persistence_score < transient_threshold
  → expected_decay = rapid

IF rumor becomes structurally relevant
  → persistence_score >= structural_threshold
  → expected_decay = slow

IF rumor concerns M&A / financing / regulation
  → claim_contains(MA_terms OR financing_terms OR regulation_terms)
  → expected_decay = persistent_candidate

IF rumor concerns one-off product chatter only
  → claim_contains(minor_product_terms) AND no_official_confirmation = true
  → expected_decay = likely_fast

IF official follow-up appears
  → confirmation_state = primary_confirmed
  → expected_decay = recompute_persistence
```

**Persistence Output:**
- `expected_decay_rate` ∈ {RAPID, MEDIUM, SLOW, PERSISTENT, DYNAMIC}

---

### 22. Evaluation Loop

```
IF rumor retained in model
  → classification NOT IN {ignore, reject_post}
  → store rumor_state

IF prediction made
  → predicted_move exists
  → track realized_move over evaluation_window

IF prediction error high
  → abs(predicted_move - realized_move) > prediction_error_threshold
  → rumor_class_model_score = rumor_class_model_score - penalty

IF prediction error low
  → abs(predicted_move - realized_move) <= prediction_error_threshold
  → rumor_class_model_score = rumor_class_model_score + reward

IF repeated forecast error high
  → rolling_mean_prediction_error(rumor_class) > rolling_error_threshold
  → downweight rumor_class

IF repeated forecast accuracy high
  → rolling_mean_prediction_error(rumor_class) <= rolling_error_threshold
  → upweight rumor_class

IF source repeatedly accurate
  → rolling_source_accuracy >= source_accuracy_threshold
  → source_score = source_score + source_reward

IF source repeatedly inaccurate
  → rolling_source_accuracy < source_accuracy_threshold
  → source_score = source_score - source_penalty
```

**Evaluation Output:**
- `rumor_class_weight` = updated by performance
- `source_score` = updated by historical accuracy

---

### 23. Output Controls

```
IF confidence high
  → overall_confidence >= high_confidence_threshold
  → output_mode = decisive

IF confidence medium
  → medium_confidence_threshold <= overall_confidence < high_confidence_threshold
  → output_mode = probabilistic

IF confidence low
  → overall_confidence < medium_confidence_threshold
  → output_mode = uncertain

IF benchmark context dominates
  → interpretation_state = benchmark_dominant
  → output_priority = benchmark_first

IF rumor-specific effect dominates
  → interpretation_state = idiosyncratic_dominant
  → output_priority = rumor_first

IF classification = freeze
  → classification == freeze
  → output_action = wait_for_confirmation

IF classification = ignore
  → classification == ignore
  → output_action = no_signal

IF classification IN {bullish_rumor_signal, bearish_rumor_signal, benchmark_regime_signal, relative_alpha_signal}
  → output_action = emit_signal

IF classification = manipulation_watch
  → classification == manipulation_watch
  → output_action = monitor_for_abuse

IF classification = upgraded_to_confirmed_event
  → classification == upgraded_to_confirmed_event
  → output_action = transition_to_news_event_logic
```

**Output Control:**
- `output_mode` ∈ {DECISIVE, PROBABILISTIC, UNCERTAIN}
- `output_priority` ∈ {BENCHMARK_FIRST, RUMOR_FIRST}
- `output_action` ∈ {EMIT_SIGNAL, WAIT_FOR_CONFIRMATION, MONITOR_ABUSE, TRANSITION_TO_NEWS, SUPPRESS}

---

### 24. Final Composite Score

```
IF composite score calculated
  → composite_score = w1*credibility + w2*relevance + w3*novelty + w4*confirmation + w5*propagation_quality + w6*impact - w7*ambiguity - w8*manipulation_risk - w9*bot_risk
  → state = scored_rumor

IF composite score above bullish threshold
  → composite_score >= bullish_score_threshold AND sentiment_state = positive
  → final_signal = bullish

IF composite score below bearish threshold
  → composite_score <= bearish_score_threshold AND sentiment_state = negative
  → final_signal = bearish

IF composite score in neutral band
  → bearish_score_threshold < composite_score < bullish_score_threshold
  → final_signal = neutral_or_watch

IF benchmark override active
  → benchmark_risk_state = drawdown AND impact_link_state = benchmark_linked
  → final_signal = benchmark_override

IF official denial override active
  → confirmation_state = officially_denied
  → final_signal = denial_override

IF manipulation override active
  → classification = manipulation_watch AND abuse_state != null
  → final_signal = suppress_or_discount
```

**Final Output:**
```json
{
  "ticker": "TSLA",
  "claim": "Rumor: Tesla planning acquisition of energy startup",
  "source_account": "@insider_trader",
  "source_type": "anonymous",
  "source_credibility": 0.45,
  "posted_time": "2026-03-28T14:25:00Z",
  "content_type": "rumor",
  "claim_state": "market_relevant",
  "claim_scope": "company",
  "claim_specificity": "specific",
  "sentiment_state": "positive",
  "sentiment_score": 0.68,
  "sentiment_confidence": 0.72,
  "novelty_state": "high",
  "surprise_state": "high",
  "confirmation_state": "unconfirmed",
  "independent_confirmations": 0,
  "impact_scope": "single_name",
  "impact_magnitude": "medium",
  "time_horizon": "multi_day",
  "expected_decay_rate": "medium",
  "propagation_state": "fast_spread",
  "engagement_velocity": 450,
  "breadth_score": 0.72,
  "bot_probability": 0.15,
  "abuse_state": "pump_risk",
  "abuse_probability": 0.62,
  "relative_state": "aligned_positive",
  "signal_type": "alpha",
  "interpretation_state": "idiosyncratic_dominant",
  "classification": "manipulation_watch",
  "composite_score": 0.58,
  "final_signal": "suppress_or_discount",
  "confidence": 0.58,
  "output_mode": "probabilistic",
  "output_action": "monitor_for_abuse",
  "ambiguity_state": "clear"
}
```

---

## INTERACTION BETWEEN AGENTS

### Flow Diagram

```
News Article from Reuters
  ↓
DisclosureResearchAgent (22 steps)
  Classifies: earnings, company-specific, official, primary_confirmed, bullish
  Output: {ticker: AAPL, signal: bullish, confidence: 0.85, ...}
  ↓
[SIMULTANEOUSLY]
  ↓
Social Media posts on Twitter about same event
  ↓
SocialRumorAgent (24 steps)
  Classifies: rumor, company-specific, weak confirmation, bullish
  Output: {ticker: AAPL, signal: bullish, confidence: 0.65, ...}
  ↓
[CONVERGENCE]
  ↓
Both agents signal BULLISH for AAPL
SentimentAgent receives both signals:
  - News: high confidence, primary confirmed
  - Rumor: medium confidence, unconfirmed
  ↓
SentimentAgent:
  Weights news more heavily (primary source)
  Checks for holdings of AAPL
  Checks 3-month price history
  Checks benchmark regime
  Outputs: BULLISH_WITH_HIGH_CONVICTION
  ↓
ExecutionAgent receives sentiment signal
```

### Cross-Confirmation Logic

```
IF DisclosureResearchAgent.signal = bullish
  AND SocialRumorAgent.signal = bullish
  AND both ≥ medium confidence
  → combined_signal_strength *= 1.5  # amplify confirmed signals

IF DisclosureResearchAgent.signal = bullish
  AND SocialRumorAgent.signal = bearish
  → combined_signal = uncertain/watch_only  # contradiction

IF DisclosureResearchAgent.signal = bullish
  AND SocialRumorAgent.classification = manipulation_watch
  → add_risk_flag = true  # credible news with suspicious social amplification
```

---

## DATABASE SCHEMA ADDITIONS

### news_articles table (new)
```sql
CREATE TABLE news_articles (
  id INTEGER PRIMARY KEY,
  timestamp DATETIME,
  source TEXT,
  source_credibility DECIMAL(3,2),
  headline TEXT,
  body_text TEXT,
  published_time DATETIME,
  ticker TEXT,
  topic_state TEXT,  -- macro, earnings, geopolitical, regulatory, sector, company, market_structure, uncertain
  sentiment_state TEXT,  -- positive, negative, neutral, mixed, uncertain
  sentiment_score DECIMAL(3,2),
  sentiment_confidence DECIMAL(3,2),
  novelty_state TEXT,  -- high, low
  confirmation_state TEXT,  -- weak, strong, primary_confirmed, contradictory, expired
  event_type TEXT,  -- scheduled, unscheduled, breaking, follow_up, rumor, official, revision
  classification TEXT,  -- bullish_signal, bearish_signal, ignore, watch_only, etc.
  composite_score DECIMAL(3,2),
  final_signal TEXT,  -- bullish, bearish, neutral, benchmark_override
  confidence DECIMAL(3,2),
  duplicate_counter INTEGER DEFAULT 0,
  duplicate_regions TEXT,  -- JSON
  staleness_value DECIMAL(3,2),
  impact_scope TEXT,  -- marketwide, sector, single_name, peer_group
  impact_magnitude TEXT,  -- high, medium, low
  time_horizon TEXT,  -- intraday, multi_day, structural
  expected_decay_rate TEXT,  -- fast, medium, slow
  ambiguity_state TEXT,
  processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ticker_sentiment ON news_articles(ticker, sentiment_state);
CREATE INDEX idx_classification ON news_articles(classification);
CREATE INDEX idx_timestamp ON news_articles(published_time);
```

### social_rumors table (new)
```sql
CREATE TABLE social_rumors (
  id INTEGER PRIMARY KEY,
  timestamp DATETIME,
  source_account TEXT,
  source_type TEXT,  -- verified, unverified, credible, anonymous, official, professional, etc.
  source_credibility DECIMAL(3,2),
  post_text TEXT,
  posted_time DATETIME,
  ticker TEXT,
  content_type TEXT,  -- rumor, leak, opinion, primary_claim, secondary_flow, refutation, satire
  claim_state TEXT,  -- market_relevant, non_actionable
  claim_scope TEXT,  -- company, sector, marketwide
  sentiment_state TEXT,
  sentiment_score DECIMAL(3,2),
  sentiment_confidence DECIMAL(3,2),
  novelty_state TEXT,
  confirmation_state TEXT,  -- unconfirmed, weak, strong, primary_confirmed, officially_denied, contradictory
  independent_confirmations INTEGER DEFAULT 0,
  classification TEXT,  -- bullish_rumor_signal, bearish_rumor_signal, manipulation_watch, freeze, etc.
  composite_score DECIMAL(3,2),
  final_signal TEXT,  -- bullish, bearish, neutral, suppress_or_discount
  confidence DECIMAL(3,2),
  propagation_state TEXT,  -- fast_spread, high_attention, broad_distribution, synthetic, decaying
  engagement_velocity INTEGER,  -- engagements per minute
  breadth_score DECIMAL(3,2),  -- unique accounts normalized
  bot_probability DECIMAL(3,2),
  abuse_state TEXT,  -- pump_risk, dump_risk, coordination, automation, etc.
  abuse_probability DECIMAL(3,2),
  impact_scope TEXT,
  impact_magnitude TEXT,
  time_horizon TEXT,
  expected_decay_rate TEXT,
  ambiguity_state TEXT,
  processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ticker_rumor ON social_rumors(ticker, sentiment_state);
CREATE INDEX idx_confirmation_state ON social_rumors(confirmation_state);
CREATE INDEX idx_posted_time ON social_rumors(posted_time);
CREATE INDEX idx_abuse_state ON social_rumors(abuse_state);
```

### research_signals table (unified output)
```sql
CREATE TABLE research_signals (
  id INTEGER PRIMARY KEY,
  timestamp DATETIME,
  ticker TEXT,
  signal_type TEXT,  -- news, rumor, both
  signal_state TEXT,  -- bullish, bearish, neutral, ignore, watch_only, benchmark_regime, alpha
  signal_score DECIMAL(3,2),  -- composite of all inputs
  confidence DECIMAL(3,2),
  news_signal TEXT,  -- from news agent
  news_confidence DECIMAL(3,2),
  rumor_signal TEXT,  -- from rumor agent
  rumor_confidence DECIMAL(3,2),
  signal_conflict BOOLEAN,  -- true if news and rumor disagree
  impact_scope TEXT,
  impact_magnitude TEXT,
  time_horizon TEXT,
  expected_decay_rate TEXT,
  benchmark_context TEXT,
  output_priority TEXT,  -- news_first, rumor_first, balanced
  risk_flags TEXT,  -- JSON: [ambiguity, manipulation_risk, bot_risk, etc.]
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ticker_signal ON research_signals(ticker, signal_state);
CREATE INDEX idx_confidence ON research_signals(confidence);
```

---

## ERROR HANDLING & EDGE CASES

### DisclosureResearchAgent

1. **Feed Unavailable**
   - Retry with exponential backoff (max 3 retries)
   - Use cached articles if within 1 hour
   - Log unavailability

2. **Parse Failure**
   - Reject article, log error
   - Continue with next article
   - Track failure rate by source

3. **Entity Extraction Failure**
   - Mark as low_context_article
   - May still process if confidence sufficient
   - Flag for manual review if high-value source

4. **Timestamp Issues**
   - Use current time as fallback for missing timestamp
   - Use sentiment/content for relative dating
   - Log assumption

5. **Language Not Supported**
   - Queue for future translation system
   - Don't process; skip article
   - Track language distribution

### SocialRumorAgent

1. **Social Feed Down**
   - Halt ingestion, use cached rumors
   - Alert monitoring system
   - Retry every 5 minutes

2. **Bot/Manipulation High**
   - Don't suppress entirely, but discount
   - Flag for monitoring
   - Track source quality over time

3. **Rapid Confirmation Updates**
   - Monitor for confirmation state changes
   - Update existing rumor record (don't create duplicate)
   - Track confirmation progression

4. **Saturation / Crowding**
   - Record crowding metrics
   - Don't discard; let SentimentAgent weight it lower
   - Use crowding as indicator of attention/momentum

5. **Sarcasm / Satire Detection**
   - If sarcasm probability > 0.8, suppress signal
   - Flag content type as satire
   - Don't penalize source for false signal

---

**Version:** 2.0 (Complete Specification, Pre-Optimization)
**Last Updated:** March 28, 2026
**Next Steps:** Map to specific agents, optimize for Pi 2W, assign responsibilities, implement

