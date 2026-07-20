# Conversational Cart State-Synchronization Test Cases

[English](cart_state_sync_cases.md) | [简体中文](cart_state_sync_cases.zh-CN.md)

## Goal

Verify that cart snapshots returned by the backend cart tool over SSE update the iOS client's `cartItems` immediately after conversational additions, removals, quantity changes, and checkout.

## Preconditions

- The backend is running and `/health` returns `{"status":"ok"}`.
- The iOS app is connected to that backend.
- A recommendation turn has produced at least two product cards.

## Case 1: add a product

1. Enter `推荐 2000 元以内的蓝牙耳机`.
2. Wait for product cards.
3. Enter `把第一个加入购物车`.
4. Verify that:
   - The guide confirms the addition.
   - The Cart tab shows the product from the first card.
   - Quantity is one and price matches its SKU price.

## Case 2: change quantity

1. Continue from case 1 and enter `第一件改成 3 个`.
2. Verify that:
   - The guide confirms the update.
   - The Cart tab shows quantity three.
   - The total reflects three units.

## Case 3: remove the second product

1. Add two different products through conversation.
2. Enter `删掉第二个`.
3. Verify that:
   - The guide confirms removal.
   - Only the first product remains in the Cart tab.
   - The total includes only the remaining product.

## Case 4: clear after checkout

1. Ensure the cart contains at least one item.
2. Enter `下单吧，地址用默认的`.
3. Verify that:
   - The guide reports the order number, item count, total, and default address.
   - The Cart tab returns to its empty state.

## Regression checks

- Hydrating `cart.lines` from a `tool_result` must not block streamed text tokens.
- A checkout action must emit an empty cart snapshot even when no `cart.lines` field is present.
- Updating local cart state must not break navigation from existing product cards to product details.
