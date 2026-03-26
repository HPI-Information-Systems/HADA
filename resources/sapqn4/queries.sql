SELECT
SalesOrganization,
SalesOrganizationName,
CreationDateYearMonth,
DisplayCurrency,
Sum(NetAmountInDisplayCurrency)
FROM SAPQN4.CSDSLSORDERITEMQ_MODIFIED( 'E', 'EUR' )
where mandt = '715'
group by
SalesOrganization,
SalesOrganizationName,
CreationDateYearMonth,
DisplayCurrency
Order by CreationDateYearMonth,SalesOrganization
WITH HINT(IGNORE_PLAN_CACHE);
