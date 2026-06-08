import SwiftUI

struct CartView: View {
    @Binding var items: [CartItem]
    @State private var editingSpecificationItem: CartSpecificationEditContext?
    @State private var draftSpecificationOptions: [String: String] = [:]
    @State private var specificationEditorDetent: PresentationDetent = .large
    @State private var selectedProduct: Product?
    @State private var selectedItemIDs: Set<String> = []
    @State private var knownItemIDs: Set<String> = []

    private let cartService = RESTProductService()

    private var itemCount: Int {
        items.reduce(0) { $0 + $1.quantity }
    }

    private var selectedItems: [CartItem] {
        items.filter { selectedItemIDs.contains($0.id) }
    }

    private var selectedItemCount: Int {
        selectedItems.reduce(0) { $0 + $1.quantity }
    }

    private var selectedTotal: Double {
        selectedItems.reduce(0) { partialResult, item in
            partialResult + item.product.priceValue(for: item.selectedOptions) * Double(item.quantity)
        }
    }

    private var selectedTotalText: String {
        formattedPrice(selectedTotal)
    }

    private var currentItemIDs: Set<String> {
        Set(items.map(\.id))
    }

    private var allItemsSelected: Bool {
        !items.isEmpty && currentItemIDs.isSubset(of: selectedItemIDs)
    }

    private var isPartiallySelected: Bool {
        !selectedItemIDs.intersection(currentItemIDs).isEmpty && !allItemsSelected
    }

    private var bottomOverlayHeight: CGFloat {
        AppTheme.cartCheckoutBottomPadding + 126
    }

    private func formattedPrice(_ value: Double) -> String {
        if value.rounded(.towardZero) == value {
            return "¥\(Int(value))"
        }
        return "¥\(String(format: "%.2f", value))"
    }

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                VStack(spacing: 0) {
                    if items.isEmpty {
                        ContentUnavailableView("购物车为空", systemImage: "cart", description: Text("从导购推荐里加入商品后会显示在这里。"))
                            .padding(.bottom, 90)
                    } else {
                        ScrollView(showsIndicators: false) {
                            VStack(spacing: 12) {
                                ForEach($items) { $item in
                                    CartRow(
                                        item: $item,
                                        isSelected: selectedItemIDs.contains(item.id),
                                        onToggleSelection: {
                                            toggleSelection(for: item.id)
                                        },
                                        onShowDetail: {
                                            selectedProduct = item.product
                                        },
                                        onEditSpecifications: {
                                            openSpecificationEditor(for: item)
                                        },
                                        onQuantityChange: { quantity in
                                            updateQuantity(id: item.id, quantity: quantity)
                                        },
                                        onRemove: {
                                            removeItem(id: item.id)
                                        }
                                    )
                                }
                            }
                            .padding(18)
                            .padding(.bottom, bottomOverlayHeight + 24)
                        }
                    }
                }

                if !items.isEmpty {
                    checkoutOverlay
                }
            }
            .sheet(item: $editingSpecificationItem) { editingItem in
                CartSpecificationEditorSheet(
                    product: editingItem.product,
                    selectedOptions: $draftSpecificationOptions,
                    onConfirm: confirmSpecificationEdit
                )
                .presentationDetents([.medium, .large], selection: $specificationEditorDetent)
                .presentationDragIndicator(.visible)
                .presentationCornerRadius(28)
            }
            .background(AppTheme.background)
            .navigationTitle(itemCount > 0 ? "购物车(\(itemCount))" : "购物车")
            .navigationDestination(item: $selectedProduct) { product in
                ProductDetailView(product: product) { product, selectedOptions, quantity in
                    addToCart(product, selectedOptions: selectedOptions, quantity: quantity)
                }
            }
            .onAppear {
                syncSelectionWithItems()
            }
            .onChange(of: items) { _, _ in
                syncSelectionWithItems()
            }
        }
    }

    private var checkoutOverlay: some View {
        CartCheckoutBar(
            selectedCount: selectedItemCount,
            totalText: selectedTotalText,
            isAllSelected: allItemsSelected,
            isPartiallySelected: isPartiallySelected,
            onToggleAll: toggleSelectAll
        )
        .padding(.horizontal, 20)
        .padding(.top, 12)
        .padding(.bottom, 12)
        .floatingLiquidPanel(cornerRadius: 26)
        .padding(.horizontal, 16)
        .padding(.bottom, AppTheme.cartCheckoutBottomPadding)
    }

    private func openSpecificationEditor(for item: CartItem) {
        draftSpecificationOptions = normalizedOptions(
            for: item.product,
            selectedOptions: item.selectedOptions
        )
        specificationEditorDetent = .large
        editingSpecificationItem = CartSpecificationEditContext(
            id: item.id,
            product: item.product
        )
    }

    private func confirmSpecificationEdit() {
        guard
            let editingSpecificationItem,
            let sourceIndex = items.firstIndex(where: { $0.id == editingSpecificationItem.id })
        else {
            closeSpecificationEditor()
            return
        }

        let sourceItem = items[sourceIndex]
        let updatedOptions = normalizedOptions(
            for: sourceItem.product,
            selectedOptions: draftSpecificationOptions
        )
        let updatedItem = CartItem(
            product: sourceItem.product,
            selectedOptions: updatedOptions,
            quantity: sourceItem.quantity,
            backendCartItemID: sourceItem.backendCartItemID,
            skuID: sourceItem.product.matchingSKU(for: updatedOptions)?.id ?? sourceItem.product.displaySKU(for: updatedOptions)?.id
        )
        let sourceWasSelected = selectedItemIDs.contains(sourceItem.id)
        let targetWasSelected = selectedItemIDs.contains(updatedItem.id)

        if updatedItem.id != sourceItem.id {
            if let existingIndex = items.firstIndex(where: { $0.id == updatedItem.id }) {
                items[existingIndex].quantity += sourceItem.quantity
                items.remove(at: sourceIndex)
                selectedItemIDs.remove(sourceItem.id)
                if sourceWasSelected || targetWasSelected {
                    selectedItemIDs.insert(updatedItem.id)
                }
            } else {
                items[sourceIndex] = updatedItem
                selectedItemIDs.remove(sourceItem.id)
                if sourceWasSelected {
                    selectedItemIDs.insert(updatedItem.id)
                }
            }
            knownItemIDs = currentItemIDs
            if let cartItemID = sourceItem.backendCartItemID,
               let skuID = updatedItem.skuID {
                syncBackendCart(
                    CartMutationRequest(
                        action: .updateSpecification,
                        productID: sourceItem.product.id,
                        skuID: skuID,
                        cartItemID: cartItemID,
                        selectedOptions: updatedOptions,
                        quantity: sourceItem.quantity
                    )
                )
            }
        }

        closeSpecificationEditor()
    }

    private func closeSpecificationEditor() {
        editingSpecificationItem = nil
        draftSpecificationOptions = [:]
    }

    private func addToCart(
        _ product: Product,
        selectedOptions: [String: String],
        quantity: Int
    ) {
        let normalizedSelectedOptions = normalizedOptions(
            for: product,
            selectedOptions: selectedOptions
        )
        let item = CartItem(
            product: product,
            selectedOptions: normalizedSelectedOptions,
            quantity: quantity,
            skuID: product.matchingSKU(for: normalizedSelectedOptions)?.id ?? product.displaySKU(for: normalizedSelectedOptions)?.id
        )
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            items[index].quantity += quantity
        } else {
            items.append(item)
        }
        selectedItemIDs.insert(item.id)
        syncBackendCart(
            CartMutationRequest(
                action: .add,
                productID: product.id,
                skuID: item.skuID,
                selectedOptions: normalizedSelectedOptions,
                quantity: quantity
            )
        )
    }

    private func toggleSelection(for id: String) {
        if selectedItemIDs.contains(id) {
            selectedItemIDs.remove(id)
        } else {
            selectedItemIDs.insert(id)
        }
    }

    private func toggleSelectAll() {
        if allItemsSelected {
            selectedItemIDs.subtract(currentItemIDs)
        } else {
            selectedItemIDs.formUnion(currentItemIDs)
        }
    }

    private func removeItem(id: String) {
        guard let item = items.first(where: { $0.id == id }) else { return }
        items.removeAll { $0.id == id }
        selectedItemIDs.remove(id)
        knownItemIDs.remove(id)
        if let cartItemID = item.backendCartItemID {
            syncBackendCart(
                CartMutationRequest(action: .remove, cartItemID: cartItemID),
                restoreOnFailure: item
            )
        }
    }

    private func updateQuantity(id: String, quantity: Int) {
        guard let item = items.first(where: { $0.id == id }),
              let cartItemID = item.backendCartItemID
        else { return }
        syncBackendCart(
            CartMutationRequest(
                action: .updateQuantity,
                cartItemID: cartItemID,
                quantity: quantity
            )
        )
    }

    private func syncBackendCart(
        _ mutation: CartMutationRequest,
        restoreOnFailure: CartItem? = nil
    ) {
        Task {
            do {
                let snapshot = try await cartService.mutateAgentCart(mutation)
                await MainActor.run {
                    applyCartSnapshot(snapshot)
                }
            } catch {
                guard let restoreOnFailure else { return }
                await MainActor.run {
                    if !items.contains(where: { $0.id == restoreOnFailure.id }) {
                        items.append(restoreOnFailure)
                    }
                    syncSelectionWithItems()
                }
            }
        }
    }

    private func applyCartSnapshot(_ snapshot: CartSnapshotPayload) {
        items = snapshot.items.map(CartItem.init(payload:))
        syncSelectionWithItems()
    }

    private func syncSelectionWithItems() {
        let newItemIDs = currentItemIDs.subtracting(knownItemIDs)
        selectedItemIDs.formIntersection(currentItemIDs)
        selectedItemIDs.formUnion(newItemIDs)
        knownItemIDs = currentItemIDs
    }

    private func normalizedOptions(
        for product: Product,
        selectedOptions: [String: String]
    ) -> [String: String] {
        product.specifications.reduce(into: [:]) { result, specification in
            if let selected = selectedOptions[specification.name] {
                result[specification.name] = selected
            } else if let fallback = specification.options.first {
                result[specification.name] = fallback
            }
        }
    }
}

private struct CartSpecificationEditContext: Identifiable {
    let id: String
    let product: Product
}

struct CartRow: View {
    @Binding var item: CartItem
    let isSelected: Bool
    let onToggleSelection: () -> Void
    let onShowDetail: () -> Void
    let onEditSpecifications: () -> Void
    let onQuantityChange: (Int) -> Void
    let onRemove: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            CartSelectionButton(
                isSelected: isSelected,
                accessibilityLabel: isSelected ? "取消选择 \(item.product.title)" : "选择 \(item.product.title)",
                action: onToggleSelection
            )
            .padding(.top, 22)

            ProductRemoteImage(url: item.product.imageURL, cornerRadius: 16, placeholderIcon: "shippingbox")
                .frame(width: 78, height: 78)
                .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .onTapGesture(perform: onShowDetail)
                .accessibilityAddTraits(.isButton)

            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 10) {
                    Text(item.product.title)
                        .font(.headline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: onShowDetail)
                        .accessibilityAddTraits(.isButton)

                    Button(action: onRemove) {
                        Image(systemName: "trash")
                            .foregroundStyle(AppTheme.textSecondary)
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.plain)
                }

                if !item.product.reason.isEmpty {
                    Text(item.product.reason)
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: onShowDetail)
                        .accessibilityAddTraits(.isButton)
                }

                if !item.specificationSummary.isEmpty {
                    Button(action: onEditSpecifications) {
                        HStack(spacing: 4) {
                            Text(item.specificationSummary)
                                .lineLimit(1)

                            Image(systemName: "chevron.right")
                                .font(.system(size: 10, weight: .semibold))
                        }
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(AppTheme.textSecondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("修改规格，当前 \(item.specificationSummary)")
                }

                HStack(alignment: .center, spacing: 10) {
                    Text(item.product.priceDisplay(for: item.selectedOptions))
                        .font(.headline)
                        .foregroundStyle(AppTheme.error)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: onShowDetail)
                        .accessibilityAddTraits(.isButton)

                    QuantityControl(
                        quantity: Binding(
                            get: { item.quantity },
                            set: { quantity in
                                item.quantity = quantity
                                onQuantityChange(quantity)
                            }
                        )
                    )
                        .contentShape(Rectangle())
                        .onTapGesture {}
                }
            }
        }
        .contentShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .padding(12)
        .floatingLiquidPanel(cornerRadius: 22)
        .overlay(
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .stroke(isSelected ? AppTheme.primary.opacity(0.42) : Color.clear, lineWidth: 1.2)
        )
        .animation(.easeOut(duration: 0.16), value: isSelected)
    }

}

private struct CartSelectionButton: View {
    let isSelected: Bool
    var isMixed: Bool = false
    var title: String?
    let accessibilityLabel: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbolName)
                    .font(.system(size: 24, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(isSelected || isMixed ? AppTheme.primary : AppTheme.textSecondary.opacity(0.72))
                    .frame(width: 28, height: 28)

                if let title {
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                }
            }
            .frame(minWidth: title == nil ? 38 : 0, minHeight: 38)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var symbolName: String {
        if isMixed {
            return "minus.circle.fill"
        }
        return isSelected ? "checkmark.circle.fill" : "circle"
    }
}

private struct CartCheckoutBar: View {
    let selectedCount: Int
    let totalText: String
    let isAllSelected: Bool
    let isPartiallySelected: Bool
    let onToggleAll: () -> Void

    private var hasSelection: Bool {
        selectedCount > 0
    }

    var body: some View {
        HStack(spacing: 14) {
            CartSelectionButton(
                isSelected: isAllSelected,
                isMixed: isPartiallySelected,
                title: "全选",
                accessibilityLabel: isAllSelected ? "取消全选" : "全选商品",
                action: onToggleAll
            )

            VStack(alignment: .leading, spacing: 4) {
                Text(hasSelection ? "已选 \(selectedCount) 件" : "请选择商品")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.textSecondary)
                Text(totalText)
                    .font(.title3.bold())
                    .foregroundStyle(hasSelection ? AppTheme.primary : AppTheme.textSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: {}) {
                Text(hasSelection ? "结算(\(selectedCount))" : "结算")
                    .font(.headline)
                    .foregroundStyle(.white)
                    .frame(minWidth: 92)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 13)
                    .background(
                        hasSelection ? AppTheme.primary : AppTheme.textSecondary.opacity(0.36),
                        in: Capsule()
                    )
            }
            .buttonStyle(.plain)
            .disabled(!hasSelection)
        }
    }
}

private struct CartSpecificationEditorSheet: View {
    let product: Product
    @Binding var selectedOptions: [String: String]
    let onConfirm: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    specificationControls
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 104)
            }
            .background(AppTheme.background)
            .safeAreaInset(edge: .bottom) {
                Button(action: onConfirm) {
                    Text("确认修改规格")
                        .font(.headline)
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 15)
                        .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 12)
                .background(.regularMaterial)
            }
            .navigationTitle("修改规格")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(LinearGradient(colors: [AppTheme.softPurple, AppTheme.softBlue], startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 72, height: 72)
                .overlay {
                    Image(systemName: "shippingbox")
                        .font(.system(size: 26, weight: .semibold))
                        .foregroundStyle(AppTheme.primary)
                }

            VStack(alignment: .leading, spacing: 6) {
                Text(product.priceDisplay(for: selectedOptions))
                    .font(.title3.bold())
                    .foregroundStyle(AppTheme.error)
                Text(product.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(2)
                Text(selectedSpecificationSummary.isEmpty ? "请选择规格" : selectedSpecificationSummary)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(1)
            }
        }
        .padding(14)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var specificationControls: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(product.specifications) { specification in
                VStack(alignment: .leading, spacing: 10) {
                    Text(specification.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)

                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 82), spacing: 10)], spacing: 10) {
                        ForEach(specification.options, id: \.self) { option in
                            CartSpecificationOptionButton(
                                title: option,
                                isSelected: selectedOptions[specification.name] == option
                            ) {
                                selectedOptions[specification.name] = option
                            }
                        }
                    }
                }
            }
        }
        .padding(16)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key)：\($0.value)" }
            .joined(separator: "  ")
    }
}

private struct CartSpecificationOptionButton: View {
    let title: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(isSelected ? AppTheme.primary : AppTheme.textPrimary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .padding(.horizontal, 8)
                .background(
                    isSelected ? AppTheme.primary.opacity(0.12) : AppTheme.surface,
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .stroke(isSelected ? AppTheme.primary : AppTheme.border, lineWidth: isSelected ? 1.2 : 0.7)
                )
        }
        .buttonStyle(.plain)
    }
}

struct QuantityControl: View {
    @Binding var quantity: Int
    @State private var isEditing = false
    @State private var draftQuantity = ""
    @FocusState private var isFocused: Bool

    private let minQuantity = 1
    private let maxQuantity = 99

    var body: some View {
        HStack(spacing: 0) {
            quantityButton(systemName: "minus") {
                quantity = max(minQuantity, quantity - 1)
            }
            .disabled(quantity <= minQuantity)
            .opacity(quantity <= minQuantity ? 0.35 : 1)

            Divider()
                .frame(height: 20)

            Group {
                if isEditing {
                    TextField("", text: $draftQuantity)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.center)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                        .focused($isFocused)
                        .onChange(of: draftQuantity) { _, newValue in
                            let filtered = newValue.filter(\.isNumber)
                            if filtered != newValue {
                                draftQuantity = filtered
                            }
                        }
                        .onChange(of: isFocused) { _, focused in
                            if !focused {
                                commitDraft()
                            }
                        }
                        .toolbar {
                            ToolbarItemGroup(placement: .keyboard) {
                                Spacer()
                                Button("完成") {
                                    commitDraft()
                                    isFocused = false
                                }
                            }
                        }
                } else {
                    Button {
                        beginEditing()
                    } label: {
                        Text("\(quantity)")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("修改数量，当前 \(quantity)")
                }
            }
            .frame(width: 42, height: 32)

            Divider()
                .frame(height: 20)

            quantityButton(systemName: "plus") {
                quantity = min(maxQuantity, quantity + 1)
            }
            .disabled(quantity >= maxQuantity)
            .opacity(quantity >= maxQuantity ? 0.35 : 1)
        }
        .frame(width: 116, height: 34)
        .background(AppTheme.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(AppTheme.border, lineWidth: 0.6)
        )
    }

    private func quantityButton(systemName: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .frame(width: 36, height: 32)
        }
        .buttonStyle(.plain)
    }

    private func beginEditing() {
        draftQuantity = "\(quantity)"
        withAnimation(.easeOut(duration: 0.12)) {
            isEditing = true
        }
        DispatchQueue.main.async {
            isFocused = true
        }
    }

    private func commitDraft() {
        let parsed = Int(draftQuantity) ?? quantity
        quantity = min(max(parsed, minQuantity), maxQuantity)
        draftQuantity = "\(quantity)"
        isEditing = false
    }
}
